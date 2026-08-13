"""Deterministic pre-prod screenshot capture for the Testing Agent.

Not left to the LLM to write its own Playwright script each run -- same
rationale as implementation.py's checkout/commit and deployment.py's merge
being done in plain Python rather than prose instructions: this always
runs the same way, doesn't burn agent turns getting a browser script
right, and can't be skipped by an agent that decides testing infra "isn't
configured." Testing Agent gets the resulting screenshot path (or the
failure reason) handed to it as context; it never drives Playwright
itself, and never gets any tool that could.
"""

from pathlib import Path


async def capture(url: str, out_path: Path, timeout_ms: int = 20000) -> tuple[bool, str]:
    """Navigates to `url` in headless Chromium and saves a full-page PNG to
    `out_path`. Returns (ok, message) -- message is a human-readable error
    on failure, or the same out_path (as a string) on success.

    Best-effort by design, same as every other optional-capability path in
    this codebase (TTS, feedback instrumentation): a missing browser or an
    unreachable URL degrades to "no screenshot this run," never a crashed
    cycle."""
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
