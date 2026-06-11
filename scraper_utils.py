"""
scraper_utils.py — Playwright-based scraper with graceful bs4 fallback.

Why Playwright instead of requests/bs4?
  • Executes JavaScript → works with SPAs and lazy-loaded prices.
  • Passes browser fingerprint checks (Cloudflare, etc.).
  • Still fails on network-level IP allowlists (e.g. motocard.com blocks all
    datacenter IPs with "Host not in allowlist"). That will work fine when
    running on a Raspberry Pi at home on a residential ISP.
"""

from __future__ import annotations

import re
import time
import random
import logging

log = logging.getLogger(__name__)

# ── Price parser (shared by both strategies) ──────────────────────────────────

def parse_price(text: str) -> float | None:
    """
    Extract a float from messy price strings.
    Handles: '199,99 €', '$299.00', '1.299,99', '€ 49.90', etc.
    """
    text = text.strip()
    # Remove currency symbols, whitespace, and non-breaking spaces
    text = re.sub(r"[€$£¥\u00a0\s]", "", text)
    # Keep only the numeric part (digits, dots, commas)
    m = re.search(r"[\d.,]+", text)
    if not m:
        return None
    raw = m.group(0)
    # European decimal: last separator is ',' followed by exactly 2 digits
    if re.search(r",\d{2}$", raw):
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError:
        return None


# ── Playwright scraper (primary) ──────────────────────────────────────────────

def _scrape_playwright(url: str, css_class: str) -> tuple[float | None, str | None]:
    """
    Launch a headless Chromium browser, load the page fully (JS executed),
    and extract the price from the element matching *css_class*.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return None, "playwright not installed — run: pip install playwright && playwright install chromium"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",   # important on Pi (low /dev/shm)
                    "--disable-gpu",
                ],
            )
            ctx = browser.new_context(
                locale="es-ES",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = ctx.new_page()

            # Block images/fonts/media to speed things up
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "media", "font", "stylesheet")
                else route.continue_(),
            )

            resp = page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            if resp is None:
                browser.close()
                return None, "No response received from the page"

            if resp.status == 403:
                body = page.content()
                detail = "Host not in allowlist" if "allowlist" in body else f"HTTP 403"
                browser.close()
                return None, (
                    f"The website blocked the request ({detail}). "
                    "This is a network-level IP block — it only affects datacenter/cloud IPs. "
                    "Running PriceTracker on your Raspberry Pi at home will work fine."
                )

            if resp.status >= 400:
                browser.close()
                return None, f"HTTP {resp.status} error fetching the page"

            # Wait a moment for any lazy-loaded price JS to settle
            try:
                page.wait_for_selector(f".{css_class}", timeout=8_000)
            except PWTimeout:
                # Element didn't appear — collect nearby class names for a useful hint
                all_classes = page.evaluate("""
                    () => [...new Set(
                        [...document.querySelectorAll('[class]')]
                        .flatMap(el => [...el.classList])
                        .filter(c => /price|precio|cost|amount/i.test(c))
                    )].slice(0, 10)
                """)
                hint = f". Price-related classes found: {all_classes}" if all_classes else ""
                browser.close()
                return None, f"Element with class '{css_class}' not found on the page{hint}"

            el = page.query_selector(f".{css_class}")
            if not el:
                browser.close()
                return None, f"Element with class '{css_class}' disappeared after waiting"

            raw_text = el.inner_text().strip()
            price = parse_price(raw_text)
            browser.close()

            if price is None:
                return None, f"Could not parse a price from: '{raw_text}'"

            return price, None

    except Exception as e:
        return None, f"Playwright error: {e}"


# ── requests/bs4 fallback (for simple pages that don't need JS) ───────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

def _scrape_requests(url: str, css_class: str) -> tuple[float | None, str | None]:
    import requests
    from bs4 import BeautifulSoup

    session = requests.Session()
    session.headers.update({
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    })
    try:
        r = session.get(url, timeout=15, allow_redirects=True)
        if r.status_code == 403:
            return None, (
                "The website blocked the request (403 Forbidden). "
                "This is likely a network-level IP block — running on your Raspberry Pi at home will work."
            )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        el = soup.find(class_=css_class)
        if not el:
            return None, f"Element with class '{css_class}' not found"
        price = parse_price(el.get_text())
        if price is None:
            return None, f"Could not parse price from: '{el.get_text().strip()}'"
        return price, None
    except Exception as e:
        return None, str(e)


# ── Public entry point ────────────────────────────────────────────────────────

def scrape_with_retry(
    url: str,
    css_class: str,
    max_retries: int = 2,
    use_playwright: bool = True,
) -> tuple[float | None, str | None]:
    """
    Scrape *url* and return (price, None) or (None, error_str).

    Strategy:
      1. Try Playwright (handles JS, anti-bot checks) up to *max_retries* times.
      2. On failure fall back to requests/bs4 once (fast, for simple static pages).

    The caller (app.py) always calls this function — it never calls
    _scrape_playwright or _scrape_requests directly.
    """
    last_err: str = "No attempts made"

    if use_playwright:
        for attempt in range(max_retries):
            if attempt > 0:
                time.sleep(2 ** attempt)
            price, err = _scrape_playwright(url, css_class)
            if price is not None:
                return price, None
            last_err = err or "Unknown Playwright error"
            log.warning("Playwright attempt %d/%d failed: %s", attempt + 1, max_retries, last_err)

            # IP-level blocks won't be fixed by retrying
            if "allowlist" in last_err or "network-level" in last_err or "HTTP 4" in last_err:
                return None, last_err

    # Fallback to requests/bs4
    log.info("Falling back to requests/bs4 for %s", url)
    price, err = _scrape_requests(url, css_class)
    if price is not None:
        return price, None

    # Return the Playwright error if it's more descriptive
    return None, last_err if use_playwright else err