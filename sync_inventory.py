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


async def download_inventory() -> bytes:
    state = base64.b64decode(required("BI_STORAGE_STATE_B64"))
    page_url = required("BI_PAGE_URL")
    export_url = required("BI_EXPORT_URL")

    with tempfile.TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / "state.json"
        state_path.write_bytes(state)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=str(state_path))
            page = await context.new_page()
            await page.goto(page_url, wait_until="domcontentloaded", timeout=120_000)
            await page.wait_for_timeout(10_000)

            token = ""
            for cookie in await context.cookies():
                if cookie["name"] == "fine_auth_token" and "tcl.com" in cookie["domain"]:
                    token = cookie["value"]
                    break
            if not token:
                await browser.close()
                raise RuntimeError("BI login session is unavailable")

            response = await context.request.get(
                export_url,
                headers={"Authorization": f"Bearer {token}", "Referer": page_url},
                timeout=120_000,
            )
            content = await response.body()
            status = response.status
            await browser.close()

    if status != 200:
        raise RuntimeError(f"BI export failed with HTTP {status}")
    if not content.startswith(b"PK"):
        raise RuntimeError("BI export did not return an Excel file")
    return content


def upload_inventory(content: bytes) -> dict:
    payload = json.dumps(
        {"content_base64": base64.b64encode(content).decode("ascii")},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        required("SCF_UPLOAD_URL"),
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {required('SYNC_SECRET')}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Cloud upload failed with HTTP {exc.code}") from exc
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
