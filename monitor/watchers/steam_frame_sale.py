"""Tier 1: Steam Frame hardware/sale page via Playwright scroll-capture.

Uses the same approach as steamframe-check/steamframe_monitor.py:
render JS, wait for sale-display, scroll to mount lazy sections, hash that
region only. Incomplete/shorter captures are ignored (real updates add content).
"""

from __future__ import annotations

import re

from monitor.browser import (
    CONTENT_SELECTOR,
    BrowserError,
    find_new_signals,
    get_browser_session,
    price_matches,
    stabilize_sale_text,
)
from monitor.changelog import unified_diff
from monitor.http_client import content_hash, short_diff
from monitor.models import WatcherResult
from monitor.watchers.base import BaseWatcher

DEFAULT_URL = "https://store.steampowered.com/hardware/steamframe"


def _is_real_change(old_text: str, new_text: str) -> bool:
    """True when the unified diff has substantive added/removed lines."""
    if old_text == new_text:
        return False
    diff = unified_diff(old_text, new_text, context=0)
    if not diff or diff.startswith("(no line-level"):
        return False
    substantive = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+") or line.startswith("-"):
            body = line[1:].strip()
            if not body:
                continue
            if re.fullmatch(r"[\W_]+", body):
                continue
            substantive += 1
    return substantive > 0


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

        text = stabilize_sale_text(capture["text"])
        length = len(text)

        prev = self.state().parsed
        prev_full_raw = str(prev.get("full_text") or "")
        # Re-stabilize stored baseline so older captures compare fairly
        prev_full = (
            stabilize_sale_text(prev_full_raw) if prev_full_raw else ""
        )
        prev_len = len(prev_full) if prev_full else int(prev.get("length") or 0)
        prev_fp = (
            content_hash(prev_full)
            if prev_full
            else (prev.get("content_hash") or self.state().fingerprint)
        )

        # Catastrophic partial (steamframe-check: < 60% of baseline)
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
        new_signals = find_new_signals(prev_full, text) if prev_full else []
        critical_signal = bool(new_signals or (has_price and not prev.get("has_price")))

        # Incomplete lazy mount: slightly shorter, but only ignore when the unified
        # diff has no substantive line changes. A real Valve edit can shrink the
        # page (delete/rewritten copy) and must still alert.
        if (
            self.state().baseline_set
            and prev_len
            and length < prev_len
            and not critical_signal
            and prev_full
        ):
            shrink = prev_len - length
            small_flicker = shrink <= max(48, int(0.015 * prev_len))
            if small_flicker and not _is_real_change(prev_full, text):
                # Echo stored state exactly so apply_result treats this as no-op
                st = self.state()
                return WatcherResult(
                    watcher_id=self.id,
                    success=True,
                    fingerprint=st.fingerprint,
                    parsed=dict(st.parsed),
                    alerts=[],
                    log_message=(
                        f"{self.id}: shorter capture ignored "
                        f"({length} < baseline {prev_len}, −{shrink} chars) "
                        f"— no line-level change (incomplete scroll)"
                    ),
                )

        signals_present = find_new_signals("", text)
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
            "full_text": text,
        }

        # Same page after normalize — rewrite stored baseline once, no alert
        if (
            self.state().baseline_set
            and prev_full
            and prev_full == text
            and (
                prev.get("content_hash") != fingerprint
                or prev_full_raw != text
            )
        ):
            return WatcherResult(
                watcher_id=self.id,
                success=True,
                fingerprint=fingerprint,
                parsed=parsed,
                alerts=[],
                suppress_alerts=True,
                change_old_text=prev_full_raw or prev_full,
                change_new_text=text,
                change_summary="normalized sale capture (tracking params / action order)",
                log_message=f"{self.id}: normalized baseline (no alert)",
            )

        alerts = []
        change_old = None
        change_new = None
        change_summary = ""
        suppress = False
        log_extra = ""

        if self.state().baseline_set:
            old_preview = str(prev.get("preview") or "")
            content_changed = bool(prev_fp and prev_fp != fingerprint)

            if content_changed and prev_full:
                change_old = prev_full
                change_new = text
                real_change = _is_real_change(prev_full, text)
                change_summary = (
                    f"sale-display content changed ({prev_len} -> {length} chars)"
                )
                if new_signals:
                    change_summary += f"; new signals: {', '.join(new_signals)}"

                if new_signals:
                    alerts.append(
                        self.make_alert(
                            "Steam Frame page — reservation/purchase signal",
                            f"New signals: {', '.join(sorted(set(new_signals)))}. "
                            f"Diff hint: {short_diff(old_preview, text[:500])} "
                            f"(full diff in ~/.config/frameme/changes.log)",
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
                elif real_change:
                    alerts.append(
                        self.make_alert(
                            "Steam Frame page content changed",
                            f"sale-display changed ({prev_len} -> {length} chars). "
                            f"Diff: {short_diff(old_preview, text[:500])} "
                            f"(full diff in ~/.config/frameme/changes.log)",
                            url,
                            stop_monitoring=False,
                        )
                    )
                else:
                    suppress = True
                    log_extra = " (noise — logged, no alert)"
                    change_summary += "; trivial/noise"

        return WatcherResult(
            watcher_id=self.id,
            success=True,
            fingerprint=fingerprint,
            parsed=parsed,
            alerts=alerts,
            log_message=(
                f"{self.id}: chars={length} price={parsed['price']!r} "
                f"signals={len(signals_present)} selector={capture['selector_found']}"
                f"{log_extra}"
            ),
            change_old_text=change_old,
            change_new_text=change_new,
            change_summary=change_summary,
            suppress_alerts=suppress,
        )
