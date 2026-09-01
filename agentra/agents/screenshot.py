"""Deterministic pre-prod screenshot capture for the Testing Agent."""

import re
from pathlib import Path


async def capture(url: str, out_path: Path, timeout_ms: int = 20000) -> tuple[bool, str]:
    """Navigates to `url` in headless Chromium and saves a full-page PNG to `out_path`."""
    results = await capture_many([(url, out_path)], timeout_ms=timeout_ms)
    return results[0][1]


async def capture_many(
    targets: list[tuple[str, Path]], timeout_ms: int = 20000
) -> list[tuple[Path, tuple[bool, str]]]:
    """Screenshots several URLs in one headless Chromium session (GitHub #108 --
    one browser launch for the root shot plus one per UI acceptance criterion's
    page). Returns [(out_path, (ok, detail)), ...] in the given order; a failure
    on one target does not abort the rest."""
    if not targets:
        return []
    for _, out_path in targets:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [(p, (False, "playwright is not installed in this environment")) for _, p in targets]

    out: list[tuple[Path, tuple[bool, str]]] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                for url, out_path in targets:
                    try:
                        page = await browser.new_page(viewport={"width": 1280, "height": 800})
                        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                        await page.screenshot(path=str(out_path), full_page=True)
                        await page.close()
                        out.append((out_path, (True, str(out_path))))
                    except Exception as exc:
                        out.append((out_path, (False, f"screenshot capture failed: {exc}")))
            finally:
                await browser.close()
    except Exception as exc:
        done = {p for p, _ in out}
        out.extend((p, (False, f"screenshot capture failed: {exc}")) for _, p in targets if p not in done)
    return out


def route_slug(route: str) -> str:
    """Filesystem-safe stem for a route, e.g. "/apps/{name}/runs" -> "apps-name-runs"."""
    slug = re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-")
    return slug or "root"
