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

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        stock = self._stock_status(soup, text)
        price_match = PRICE_RE.search(text)
        price = price_match.group(0) if price_match else None
        cart_enabled = self._cart_enabled(soup)

        fingerprint = content_hash(f"{stock}|{price}|{cart_enabled}")
        parsed = {
            "stock": stock,
            "price": price,
            "cart_enabled": cart_enabled,
        }

        alerts = []
        prev = self.state().parsed
        if self.state().baseline_set:
            if prev.get("stock") and prev.get("stock") != stock:
                alerts.append(
                    self.make_alert(
                        "Komodo stock status changed",
                        f"{prev.get('stock')} -> {stock}. Note: {LEAD_NOTE}",
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

    def _stock_status(self, soup: BeautifulSoup, text: str) -> str:
        lower = text.lower()
        for needle, label in (
            ("out of stock", "out_of_stock"),
            ("sold out", "sold_out"),
            ("in stock", "in_stock"),
            ("pre-order", "preorder"),
            ("preorder", "preorder"),
            ("coming soon", "coming_soon"),
            ("unavailable", "unavailable"),
        ):
            if needle in lower:
                return label
        stock_el = soup.select_one(
            ".stock, .availability, .product-stock, .inventory_status"
        )
        if stock_el:
            return stock_el.get_text(" ", strip=True)[:80] or "unknown"
        return "unknown"

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
