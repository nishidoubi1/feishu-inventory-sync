from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.error
import urllib.request

from playwright.async_api import async_playwright


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required secret: {name}")
    return value


def login_credentials() -> tuple[str, str]:
    raw = required("BI_LOGIN")
    lines = raw.splitlines()
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        raise RuntimeError("BI_LOGIN must contain account on line 1 and password on line 2")
    return lines[0].strip(), lines[1].strip()


async def login_if_needed(page, page_url: str) -> None:
    username, password = login_credentials()
    await page.goto(page_url, wait_until="domcontentloaded", timeout=120_000)
    await page.wait_for_timeout(3000)
    cookies = await page.context.cookies()
    if any(c["name"] == "fine_auth_token" and "tcl.com" in c["domain"] for c in cookies):
        return
    fields = page.locator("input:visible")
    if await fields.count() < 2:
        raise RuntimeError("BI login page was not recognized")
    await fields.nth(0).fill(username)
    await fields.nth(1).fill(password)
    await page.get_by_text("登录", exact=True).click()
    await page.wait_for_timeout(5000)
    cookies = await page.context.cookies()
    if not any(c["name"] == "fine_auth_token" and "tcl.com" in c["domain"] for c in cookies):
        raise RuntimeError("BI login failed; check BI_LOGIN")


async def download_inventory() -> bytes:
    page_url = required("BI_PAGE_URL")
    export_url = required("BI_EXPORT_URL")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await login_if_needed(page, page_url)
        token = next(c["value"] for c in await context.cookies() if c["name"] == "fine_auth_token" and "tcl.com" in c["domain"])
        response = await context.request.get(export_url, headers={"Authorization": f"Bearer {token}", "Referer": page_url}, timeout=120_000)
        content = await response.body()
        status = response.status
        await browser.close()
    if status != 200:
        raise RuntimeError(f"BI export failed with HTTP {status}")
    if not content.startswith(b"PK"):
        raise RuntimeError("BI export did not return an Excel file")
    return content


def upload_inventory(content: bytes) -> dict:
    payload = json.dumps({"content_base64": base64.b64encode(content).decode("ascii")}, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(required("SCF_UPLOAD_URL"), data=payload, method="POST", headers={"Authorization": f"Bearer {required('SYNC_SECRET')}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Cloud upload failed with HTTP {exc.code}") from exc
    if not result.get("ok"):
        raise RuntimeError("Cloud upload did not complete")
    return result["result"]


async def main() -> None:
    result = upload_inventory(await download_inventory())
    print(f"Inventory sync complete: source={result.get('source', 0)}, created={result.get('created', 0)}, updated={result.get('updated', 0)}, batch={result.get('batch', '')}")


if __name__ == "__main__":
    asyncio.run(main())
