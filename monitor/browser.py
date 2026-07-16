"""Shared Playwright helpers for JS-rendered Steam pages.

Adapted from steamframe-check/steamframe_monitor.py: wait for sale-display,
scroll to mount lazy sections, capture fullest text + action labels.
"""

from __future__ import annotations

import re
import threading
from typing import Any


CONTENT_SELECTOR = '[data-featuretarget="sale-display"]'
RENDER_SETTLE_MS = 6000
PAGE_TIMEOUT_MS = 60_000
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

RESERVATION_SIGNALS = [
    "reserve",
    "reservation",
    "waitlist",
    "wait list",
    "join the list",
    "notify me",
    "get notified",
    "email me",
    "sign up",
    "add to cart",
    "buy now",
    "purchase",
    "checkout",
    "pre-order",
    "preorder",
    "order now",
    "in stock",
    "available now",
    "now available",
    "buy steam frame",
]


class BrowserError(RuntimeError):
    pass


def normalize(text: str) -> str:
    text = text.replace("\xa0", " ")
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(line)
    out: list[str] = []
    for line in lines:
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    return "\n".join(out).strip()


def find_new_signals(old_text: str, new_text: str) -> list[str]:
    old_l, new_l = old_text.lower(), new_text.lower()
    hits = []
    for sig in RESERVATION_SIGNALS:
        if sig in new_l and sig not in old_l:
            hits.append(sig)
    return hits


def price_matches(text: str) -> list[str]:
    return re.findall(r"\$\d{3,4}(?:\.\d{2})?", text)


class PlaywrightSession:
    """Long-lived headless Chromium for repeated Steam page captures."""

    def __init__(self, user_agent: str = DEFAULT_UA) -> None:
        self.user_agent = user_agent
        self._lock = threading.RLock()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    def _ensure_started(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError(
                "playwright is not installed. Run: "
                "pip install playwright && playwright install chromium"
            ) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        self._page = self._context.new_page()

    def stop(self) -> None:
        with self._lock:
            for obj in (self._page, self._context, self._browser):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:
                    pass
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
            self._page = self._context = self._browser = self._playwright = None

    def capture_page_text(
        self,
        url: str,
        *,
        wait_until: str = "networkidle",
        settle_ms: int = 3000,
        content_selector: str | None = None,
    ) -> dict[str, Any]:
        """Load a JS-rendered page and return normalized body/region text."""
        with self._lock:
            self._ensure_started()
            assert self._page is not None
            page = self._page

            page.goto(url, wait_until=wait_until, timeout=PAGE_TIMEOUT_MS)
            if content_selector:
                try:
                    page.wait_for_selector(content_selector, timeout=PAGE_TIMEOUT_MS)
                except Exception:
                    pass
            page.wait_for_timeout(settle_ms)

            title = page.title()
            if content_selector:
                el = page.query_selector(content_selector)
                text = el.inner_text() if el else page.inner_text("body")
            else:
                text = page.inner_text("body")

            text = normalize(text or "")
            return {
                "text": text,
                "title": title,
                "final_url": page.url,
                "length": len(text),
            }

    def capture_sale_page(
        self,
        url: str,
        *,
        content_selector: str = CONTENT_SELECTOR,
    ) -> dict[str, Any]:
        """Load URL, scroll-mount lazy content, return normalized text + meta."""
        with self._lock:
            self._ensure_started()
            assert self._page is not None
            page = self._page

            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            try:
                page.wait_for_selector(content_selector, timeout=PAGE_TIMEOUT_MS)
            except Exception:
                pass
            page.wait_for_timeout(RENDER_SETTLE_MS)

            full_text = self._capture_full(page, content_selector)
            el = page.query_selector(content_selector)
            if not el and not full_text:
                raise BrowserError(f"content region not found: {content_selector}")

            parts = [full_text or (el.inner_text() if el else "")]
            actions: list[str] = []
            if el:
                for h in el.query_selector_all("a, button, input[type=submit]"):
                    label = (h.inner_text() or h.get_attribute("value") or "").strip()
                    href = h.get_attribute("href") or ""
                    if label:
                        actions.append(
                            f"[ACTION] {label}" + (f"  ->  {href}" if href else "")
                        )
            if actions:
                seen: set[str] = set()
                uniq = [a for a in actions if not (a in seen or seen.add(a))]
                parts.append("\n--- page actions ---\n" + "\n".join(uniq))

            text = normalize("\n".join(parts))
            return {
                "text": text,
                "final_url": page.url,
                "actions": actions,
                "length": len(text),
                "selector_found": el is not None,
            }

    def _capture_full(self, page: Any, content_selector: str) -> str:
        page.evaluate("window.scrollTo(0, 0)")
        best, stagnant, last_h = "", 0, -1
        for _ in range(60):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(350)
            el = page.query_selector(content_selector)
            txt = el.inner_text() if el else ""
            if len(txt) > len(best):
                best = txt
            at_bottom = page.evaluate(
                "(window.scrollY + window.innerHeight) >= (document.body.scrollHeight - 3)"
            )
            h = page.evaluate("document.body.scrollHeight")
            if at_bottom and h == last_h:
                stagnant += 1
                if stagnant >= 5:
                    break
            else:
                stagnant = 0
            last_h = h
        return best


_session: PlaywrightSession | None = None
_session_lock = threading.Lock()


def get_browser_session(user_agent: str = DEFAULT_UA) -> PlaywrightSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = PlaywrightSession(user_agent=user_agent)
        return _session


def shutdown_browser_session() -> None:
    global _session
    with _session_lock:
        if _session is not None:
            _session.stop()
            _session = None
