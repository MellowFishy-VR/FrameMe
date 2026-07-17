"""Tier 1: Komodo Station (JP distributor) product page."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from monitor.http_client import content_hash
from monitor.models import WatcherResult
from monitor.watchers.base import BaseWatcher

PRICE_RE = re.compile(
    r"(?:JPY|USD|\$)\s?[\d,]+|[\d,]+\s?(?:JPY|yen|USD)|¥[\d,]+",
    re.I,
)
LEAD_NOTE = "komodo may lead the US announcement"
UNKNOWN = "unknown"
# Soft 200s / challenge pages are usually much smaller than the real PDP.
MIN_PAGE_CHARS = 8_000

# Order matters: more specific / negative first.
STOCK_NEEDLES: tuple[tuple[str, str], ...] = (
    ("out of stock", "out_of_stock"),
    ("sold out", "sold_out"),
    ("売り切れ", "sold_out"),
    ("在庫なし", "out_of_stock"),
    ("in stock", "in_stock"),
    ("在庫あり", "in_stock"),
    ("pre-order", "preorder"),
    ("preorder", "preorder"),
    ("予約受付", "preorder"),
    ("coming soon", "coming_soon"),
    ("近日発売", "coming_soon"),
    ("comingsoon", "coming_soon"),
    ("unavailable", "unavailable"),
)


class KomodoWatcher(BaseWatcher):
    def poll(self) -> WatcherResult:
        url = (
            self.config.url
            or "https://komodostation.com/product/steam-frame_jpy/?lang=en"
        )
        try:
            if self.http.should_check_robots(url) and not self.http.robots.allowed(url):
                return WatcherResult(
                    watcher_id=self.id,
                    success=False,
                    disable=True,
                    disable_reason=f"robots.txt disallows {url}",
                    error="robots.txt disallowed",
                    log_message=f"{self.id}: disabled by robots.txt",
                )
            resp = self.http.fetch(url)
        except Exception as exc:
            return self.handle_http_exception(exc)

        # Incomplete / challenge HTML — do not treat as a stock transition.
        if len(resp.text) < MIN_PAGE_CHARS:
            return self._keep_previous(
                f"{self.id}: short response ({len(resp.text)} chars) — parse skipped",
                http_status=resp.status,
            )

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        stock = self._stock_status(soup, text)
        price_match = PRICE_RE.search(text)
        price = price_match.group(0) if price_match else None
        cart_enabled = self._cart_enabled(soup)

        prev = self.state().parsed
        prev_stock = str(prev.get("stock") or "")

        # Ambiguous parse: keep last known stock so we never alert on
        # coming_soon <-> unknown flicker from flaky HTML.
        if stock == UNKNOWN:
            return self._keep_previous(
                f"{self.id}: stock inconclusive — keeping "
                f"{prev_stock or UNKNOWN!r}",
                http_status=resp.status,
            )

        fingerprint = content_hash(f"{stock}|{price}|{cart_enabled}")
        parsed = {
            "stock": stock,
            "price": price,
            "cart_enabled": cart_enabled,
        }

        alerts = []
        if self.state().baseline_set:
            if (
                prev_stock
                and prev_stock != stock
                and prev_stock != UNKNOWN
                and stock != UNKNOWN
            ):
                alerts.append(
                    self.make_alert(
                        "Komodo stock status changed",
                        f"{prev_stock} -> {stock}. Note: {LEAD_NOTE}",
                        url,
                    )
                )
            if price and not prev.get("price"):
                alerts.append(
                    self.make_alert(
                        "Komodo price appeared",
                        f"Price: {price}. Note: {LEAD_NOTE}",
                        url,
                    )
                )
            elif price and prev.get("price") and prev.get("price") != price:
                alerts.append(
                    self.make_alert(
                        "Komodo price changed",
                        f"{prev.get('price')} -> {price}. Note: {LEAD_NOTE}",
                        url,
                    )
                )
            if cart_enabled and not prev.get("cart_enabled"):
                alerts.append(
                    self.make_alert(
                        "Komodo add-to-cart enabled",
                        f"Add-to-cart became available. Note: {LEAD_NOTE}",
                        url,
                    )
                )

        return WatcherResult(
            watcher_id=self.id,
            success=True,
            fingerprint=fingerprint,
            parsed=parsed,
            alerts=alerts,
            log_message=(
                f"{self.id}: stock={stock!r} price={price!r} cart_enabled={cart_enabled}"
            ),
            http_status=resp.status,
        )

    def _keep_previous(self, log_message: str, *, http_status: int | None) -> WatcherResult:
        """Echo stored state so apply_result is a no-op (no alert / no log spam)."""
        st = self.state()
        if st.baseline_set and st.fingerprint:
            return WatcherResult(
                watcher_id=self.id,
                success=True,
                fingerprint=st.fingerprint,
                parsed=dict(st.parsed),
                alerts=[],
                log_message=log_message,
                http_status=http_status,
            )
        # No baseline yet — store unknown so we can seed later without alerting.
        return WatcherResult(
            watcher_id=self.id,
            success=True,
            fingerprint=content_hash(UNKNOWN),
            parsed={"stock": UNKNOWN, "price": None, "cart_enabled": False},
            alerts=[],
            log_message=log_message,
            http_status=http_status,
        )

    def _stock_status(self, soup: BeautifulSoup, text: str) -> str:
        # Prefer WooCommerce stock nodes when present.
        for sel in (
            "p.stock",
            ".stock",
            ".availability",
            ".product-stock",
            ".inventory_status",
        ):
            for el in soup.select(sel):
                label = self._label_from_text(el.get_text(" ", strip=True))
                if label != UNKNOWN:
                    return label

        label = self._label_from_text(text)
        if label != UNKNOWN:
            return label

        # Title / meta sometimes carry the only reliable phrase.
        if soup.title:
            label = self._label_from_text(soup.title.get_text(" ", strip=True))
            if label != UNKNOWN:
                return label
        for meta in soup.select('meta[name="description"], meta[property="og:description"]'):
            content = meta.get("content") or ""
            label = self._label_from_text(content)
            if label != UNKNOWN:
                return label

        return UNKNOWN

    def _label_from_text(self, text: str) -> str:
        lower = text.lower()
        for needle, label in STOCK_NEEDLES:
            if needle.isascii():
                if needle in lower:
                    return label
            elif needle in text:
                return label
        return UNKNOWN

    def _cart_enabled(self, soup: BeautifulSoup) -> bool:
        for sel in (
            "button.single_add_to_cart_button",
            "button[name='add-to-cart']",
            "form.cart button",
            ".add-to-cart",
            "button.add_to_cart",
        ):
            for el in soup.select(sel):
                disabled = el.has_attr("disabled") or el.get("aria-disabled") == "true"
                classes = " ".join(el.get("class") or [])
                if "disabled" in classes.lower():
                    disabled = True
                if not disabled:
                    return True
        return False
