"""Deterministic pre-prod screenshot capture for the Testing Agent."""

from pathlib import Path


async def capture(url: str, out_path: Path, timeout_ms: int = 20000) -> tuple[bool, str]:
    """Navigates to `url` in headless Chromium and saves a full-page PNG to `out_path`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "playwright is not installed in this environment"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 800})
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                await page.screenshot(path=str(out_path), full_page=True)
            finally:
                await browser.close()
    except Exception as exc:
        return False, f"screenshot capture failed: {exc}"
    return True, str(out_path)
