"""Tier 1: Steam Frame hardware/sale page via Playwright scroll-capture.

Uses the same approach as steamframe-check/steamframe_monitor.py:
render JS, wait for sale-display, scroll to mount lazy sections, hash that
region only, alert on content changes and new reservation signals.
"""

from __future__ import annotations

from monitor.browser import (
    CONTENT_SELECTOR,
    BrowserError,
    find_new_signals,
    get_browser_session,
    price_matches,
)
from monitor.http_client import content_hash, short_diff
from monitor.models import WatcherResult
from monitor.watchers.base import BaseWatcher

DEFAULT_URL = "https://store.steampowered.com/hardware/steamframe"


class SteamFrameSaleWatcher(BaseWatcher):
    def poll(self) -> WatcherResult:
        url = self.config.url or DEFAULT_URL
        selector = str(
            self.config.extra.get("content_selector") or CONTENT_SELECTOR
        )

        try:
            session = get_browser_session(user_agent=self.http.user_agent)
            capture = session.capture_sale_page(url, content_selector=selector)
        except BrowserError as exc:
            return WatcherResult(
                watcher_id=self.id,
                success=False,
                error=str(exc),
                retryable_error=True,
                log_message=f"{self.id}: browser error — {exc}",
            )
        except Exception as exc:
            return self.handle_http_exception(exc)

        text = capture["text"]
        length = int(capture["length"])

        # Partial capture guard (same idea as steamframe-check)
        prev = self.state().parsed
        prev_len = int(prev.get("length") or 0)
        if prev_len and length < 0.6 * prev_len:
            return WatcherResult(
                watcher_id=self.id,
                success=False,
                error=f"partial capture ({length} vs baseline {prev_len})",
                retryable_error=True,
                log_message=(
                    f"{self.id}: partial capture ({length} vs baseline {prev_len}) — skipping"
                ),
            )

        prices = price_matches(text)
        has_price = bool(prices)
        signals_present = find_new_signals("", text)  # all signals currently present
        fingerprint = content_hash(text)
        parsed = {
            "length": length,
            "content_hash": fingerprint,
            "final_url": capture["final_url"],
            "selector_found": capture["selector_found"],
            "has_price": has_price,
            "price": prices[0] if prices else None,
            "signals_present": signals_present,
            "preview": text[:500],
        }

        alerts = []
        if self.state().baseline_set:
            old_text_hash = prev.get("content_hash")
            old_preview = str(prev.get("preview") or "")
            # Reconstruct old text isn't stored fully — use preview + signal/price transitions
            # Store full text in parsed for next compare
            old_full = str(prev.get("full_text") or "")
            new_signals = find_new_signals(old_full, text) if old_full else []
            content_changed = old_text_hash and old_text_hash != fingerprint

            if new_signals:
                alerts.append(
                    self.make_alert(
                        "Steam Frame page — reservation/purchase signal",
                        f"New signals: {', '.join(sorted(set(new_signals)))}. "
                        f"Diff hint: {short_diff(old_preview, text[:500])}",
                        url,
                        stop_monitoring=True,
                    )
                )
            elif has_price and not prev.get("has_price"):
                alerts.append(
                    self.make_alert(
                        "Steam Frame price appeared",
                        f"Price string found: {parsed['price']}",
                        url,
                        stop_monitoring=True,
                    )
                )
            elif content_changed:
                alerts.append(
                    self.make_alert(
                        "Steam Frame page content changed",
                        f"sale-display changed ({prev_len} -> {length} chars). "
                        f"Diff: {short_diff(old_preview, text[:500])}",
                        url,
                        stop_monitoring=False,
                    )
                )

        # Keep full text for next signal/diff compare (state JSON)
        parsed["full_text"] = text

        return WatcherResult(
            watcher_id=self.id,
            success=True,
            fingerprint=fingerprint,
            parsed=parsed,
            alerts=alerts,
            log_message=(
                f"{self.id}: chars={length} price={parsed['price']!r} "
                f"signals={len(signals_present)} selector={capture['selector_found']}"
            ),
        )
