"""Tier 3: ImportGenius shipment pages (best-effort; disable if paywalled)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from monitor.http_client import content_hash
from monitor.models import WatcherResult
from monitor.watchers.base import BaseWatcher

COUNT_RE = re.compile(
    r"(\d[\d,]*)\s*(?:shipments?|records?|results?|imports?)",
    re.I,
)
DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.I,
)
PAYWALL_HINTS = (
    "subscribe",
    "sign up",
    "create an account",
    "premium",
    "paywall",
    "login to view",
    "log in to view",
    "unlock",
)


class ImportGeniusWatcher(BaseWatcher):
    def poll(self) -> WatcherResult:
        urls = list(self.config.urls) or [
            "https://importgenius.com/suppliers/tech-front-chongqing-computer-co",
            "https://importgenius.com/importers/valve-corp",
        ]

        page_data: dict[str, dict] = {}
        extractable = False
        paywalled_all = True

        for url in urls:
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
                # One URL failing shouldn't kill the whole watcher if another works
                page_data[url] = {"error": str(exc)}
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(" ", strip=True)
            lower = text.lower()
            looks_paywalled = any(h in lower for h in PAYWALL_HINTS) and len(text) < 4000

            count_match = COUNT_RE.search(text)
            dates = DATE_RE.findall(text)
            shipment_count = count_match.group(1).replace(",", "") if count_match else None
            latest_date = dates[0] if dates else None

            if shipment_count or latest_date:
                extractable = True
                paywalled_all = False
            elif not looks_paywalled and len(text) > 500:
                # Some visible content but no structured fields — still track hash
                paywalled_all = False

            page_data[url] = {
                "shipment_count": shipment_count,
                "latest_date": latest_date,
                "paywalled": looks_paywalled,
                "text_len": len(text),
            }

        if not extractable and paywalled_all:
            return WatcherResult(
                watcher_id=self.id,
                success=False,
                disable=True,
                disable_reason="ImportGenius pages appear fully paywalled; no extractable data",
                error="paywalled",
                log_message=(
                    f"{self.id}: WARNING — paywalled/no data; disabling watcher"
                ),
            )

        fingerprint = content_hash(str(sorted(page_data.items())))
        parsed = {"pages": page_data}

        alerts = []
        prev_pages = (self.state().parsed or {}).get("pages") or {}
        if self.state().baseline_set:
            for url, data in page_data.items():
                old = prev_pages.get(url) or {}
                if data.get("error"):
                    continue
                changes = []
                if data.get("shipment_count") != old.get("shipment_count") and (
                    data.get("shipment_count") or old.get("shipment_count")
                ):
                    changes.append(
                        f"count {old.get('shipment_count')!r} -> {data.get('shipment_count')!r}"
                    )
                if data.get("latest_date") != old.get("latest_date") and (
                    data.get("latest_date") or old.get("latest_date")
                ):
                    changes.append(
                        f"latest {old.get('latest_date')!r} -> {data.get('latest_date')!r}"
                    )
                if changes:
                    alerts.append(
                        self.make_alert(
                            "ImportGenius shipment data changed",
                            f"{url}: " + "; ".join(changes),
                            url,
                        )
                    )

        return WatcherResult(
            watcher_id=self.id,
            success=True,
            fingerprint=fingerprint,
            parsed=parsed,
            alerts=alerts,
            log_message=f"{self.id}: pages={len(page_data)} extractable={extractable}",
        )
