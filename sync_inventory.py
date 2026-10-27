from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required secret: {name}")
    return value


def prepare_state_path(temp_dir: str) -> Path:
    """优先使用工作流缓存中的登录状态；首次运行再用 Secret 初始化。"""
    configured = os.environ.get("BI_STATE_PATH", "").strip()
    state_path = Path(configured) if configured else Path(temp_dir) / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if not state_path.exists():
        encoded = os.environ.get("BI_STORAGE_STATE_B64", "").strip()
        if encoded:
            state_path.write_bytes(base64.b64decode(encoded))
    return state_path


def login_credentials() -> tuple[str, str]:
    raw = required("BI_LOGIN")
    lines = raw.splitlines()
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        raise RuntimeError("BI_LOGIN must contain account on line 1 and password on line 2")
    return lines[0].strip(), lines[1].strip()


async def download_inventory() -> bytes:
    page_url = required("BI_PAGE_URL")
    export_url = required("BI_EXPORT_URL")

    with tempfile.TemporaryDirectory() as temp_dir:
        state_path = prepare_state_path(temp_dir)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context_kwargs = {"storage_state": str(state_path)} if state_path.exists() else {}
            context = await browser.new_context(**context_kwargs)
            async def find_token(browser_context) -> str:
                for cookie in await browser_context.cookies():
                    if cookie["name"] == "fine_auth_token" and "tcl.com" in cookie["domain"]:
                        return cookie["value"]
                return ""

            async def export_with_token(browser_context, token: str):
                if not token:
                    return None, b""
                result = await browser_context.request.get(
                    export_url,
                    headers={"Authorization": f"Bearer {token}", "Referer": page_url},
                    timeout=120_000,
                )
                return result, await result.body()

            # 先尝试缓存会话。Cookie 存在不代表仍有效，必须以实际 Excel 响应判定。
            page = await context.new_page()
            await page.goto(page_url, wait_until="domcontentloaded", timeout=120_000)
            await page.wait_for_timeout(10_000)
            response, content = await export_with_token(context, await find_token(context))

            # 旧 Cookie 即使还在缓存里，也可能已失效并返回登录页。此时丢弃缓存上下文，
            # 用 BI_LOGIN 建立全新会话后重试一次，成功才保存新的状态。
            if (response is None or response.status != 200 or not content.startswith(b"PK")) and os.environ.get("BI_LOGIN", "").strip():
                fresh_context = await browser.new_context()
                fresh_page = await fresh_context.new_page()
                await fresh_page.goto(page_url, wait_until="domcontentloaded", timeout=120_000)
                await fresh_page.wait_for_timeout(8_000)
                fields = fresh_page.locator("input:visible")
                if await fields.count() < 2:
                    await browser.close()
                    raise RuntimeError("BI login page was not recognized after cached session failed")
                username, password = login_credentials()
                await fields.nth(0).fill(username)
                await fields.nth(1).fill(password)
                await fields.nth(1).press("Enter")
                await fresh_page.wait_for_timeout(8_000)
                response, content = await export_with_token(fresh_context, await find_token(fresh_context))
                if response is not None and response.status == 200 and content.startswith(b"PK"):
                    await fresh_context.storage_state(path=str(state_path))
                await fresh_context.close()
            elif response is not None and response.status == 200 and content.startswith(b"PK"):
                # 只有确认下载到有效 Excel 后才覆盖登录状态缓存。
                await context.storage_state(path=str(state_path))

            status = response.status if response is not None else 0
            content_type = response.headers.get("content-type", "unknown") if response is not None else "no-response"
            await browser.close()

    if status != 200:
        raise RuntimeError(f"BI export failed with HTTP {status} (content-type {content_type})")
    if not content.startswith(b"PK"):
        raise RuntimeError(
            "BI export did not return an Excel file "
            f"(HTTP {status}, content-type {content_type}, bytes {len(content)})"
        )
    return content


def upload_inventory(content: bytes) -> dict:
    if not content.startswith(b"PK"):
        raise RuntimeError("Refusing to upload inventory export: response is not an Excel file")
    if len(content) > 5 * 1024 * 1024:
        raise RuntimeError(
            f"Inventory Excel is too large for the cloud upload endpoint ({len(content)} bytes; limit is 5 MiB)"
        )
    request = urllib.request.Request(
        required("SCF_UPLOAD_URL"),
        data=content,
        method="POST",
        headers={
            "Authorization": f"Bearer {required('SYNC_SECRET')}",
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace").strip()
        message = f"Cloud upload failed with HTTP {exc.code}"
        if detail:
            message += f": {detail}"
        raise RuntimeError(message) from exc
    if not result.get("ok"):
        raise RuntimeError("Cloud upload did not complete")
    return result["result"]


async def main() -> None:
    content = await download_inventory()
    result = upload_inventory(content)
    print(
        "Inventory sync complete: "
        f"source={result.get('source', 0)}, "
        f"created={result.get('created', 0)}, "
        f"updated={result.get('updated', 0)}, "
        f"batch={result.get('batch', '')}"
    )


if __name__ == "__main__":
    asyncio.run(main())

