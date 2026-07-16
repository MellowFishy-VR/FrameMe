"""Tier 3: Valve hardware compliance FAQ body hash + diff.

Help pages are JS-rendered; plain HTTP only gets a login shell. Uses Playwright.
"""

from __future__ import annotations

import re

from monitor.browser import BrowserError, get_browser_session, normalize
from monitor.http_client import content_hash, short_diff
from monitor.models import WatcherResult
from monitor.watchers.base import BaseWatcher

NAV_NOISE = {
    "store",
    "community",
    "home",
    "discovery queue",
    "wishlist",
    "points shop",
    "news",
    "charts",
    "discussions",
    "workshop",
    "market",
    "broadcasts",
    "about",
    "support",
    "install steam",
    "sign in",
    "language",
    "change language",
    "get the steam mobile app",
    "view desktop website",
    "steam support",
}


class ValveComplianceWatcher(BaseWatcher):
    def poll(self) -> WatcherResult:
        url = (
            self.config.url
            or "https://help.steampowered.com/en/faqs/view/3D15-B320-C20E-BACD"
        )
        try:
            session = get_browser_session(user_agent=self.http.user_agent)
            capture = session.capture_page_text(url, wait_until="networkidle", settle_ms=3000)
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

        title = capture.get("title") or ""
        body_text = self._clean_article(str(capture.get("text") or ""), title)
        usable = self._is_usable_article(body_text)

        if not usable:
            state = self.state()
            return WatcherResult(
                watcher_id=self.id,
                success=True,
                fingerprint=state.fingerprint or content_hash("unusable"),
                parsed=state.parsed
                or {
                    "usable": False,
                    "title": title,
                    "length": len(body_text),
                    "body_hash": content_hash(body_text),
                    "body_preview": body_text[:200],
                },
                alerts=[],
                log_message=(
                    f"{self.id}: FAQ body not extractable — "
                    f"skipping alert; title={title!r} len={len(body_text)}"
                ),
            )

        digest = content_hash(body_text)
        # Track whether Steam Frame appears in the compliance list
        frame_mentioned = bool(
            re.search(r"steam frame|model\s*1015", body_text, re.I)
        )
        parsed = {
            "usable": True,
            "body_hash": digest,
            "body_preview": body_text[:800],
            "length": len(body_text),
            "title": title,
            "frame_mentioned": frame_mentioned,
        }

        alerts = []
        prev = self.state().parsed
        if (
            self.state().baseline_set
            and prev.get("usable")
            and prev.get("body_hash")
            and prev.get("body_hash") != digest
        ):
            old_preview = str(prev.get("body_preview") or "")
            snippet = short_diff(old_preview, body_text[:800])
            msg = f"Article body changed. Diff: {snippet}"
            if frame_mentioned and not prev.get("frame_mentioned"):
                msg = f"Steam Frame newly listed in compliance docs. {msg}"
            alerts.append(
                self.make_alert(
                    "Valve hardware compliance FAQ changed",
                    msg,
                    url,
                )
            )

        return WatcherResult(
            watcher_id=self.id,
            success=True,
            fingerprint=digest,
            parsed=parsed,
            alerts=alerts,
            log_message=(
                f"{self.id}: body_len={len(body_text)} hash={digest[:12]} "
                f"frame={frame_mentioned} title={title!r}"
            ),
        )

    def _clean_article(self, text: str, title: str) -> str:
        lines = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                continue
            if s.lower() in NAV_NOISE:
                continue
            lines.append(s)
        cleaned = normalize("\n".join(lines))
        if title and not cleaned.startswith(title):
            return f"{title}\n{cleaned}" if cleaned else title
        return cleaned

    def _is_usable_article(self, text: str) -> bool:
        lower = text.lower()
        if len(text) < 400:
            return False
        if "compliance documentation" not in lower and "declaration of conformity" not in lower:
            return False
        if "model" not in lower:
            return False
        return True
