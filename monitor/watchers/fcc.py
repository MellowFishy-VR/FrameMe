"""Tier 3: FCC filings for Valve grantee 2AES4."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from monitor.http_client import content_hash
from monitor.models import WatcherResult
from monitor.watchers.base import BaseWatcher

FCC_ID_RE = re.compile(r"2AES4[A-Z0-9]+", re.I)
DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b",
    re.I,
)


class FccWatcher(BaseWatcher):
    def poll(self) -> WatcherResult:
        url = self.config.url or "https://fccid.io/2AES4"
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
        text = soup.get_text("\n", strip=True)

        fcc_ids = sorted(set(FCC_ID_RE.findall(text)))
        # Collect rows that look like filings
        filings: list[dict[str, str]] = []
        for row in soup.select("tr"):
            row_text = row.get_text(" ", strip=True)
            ids = FCC_ID_RE.findall(row_text)
            if not ids:
                continue
            dates = DATE_RE.findall(row_text)
            filings.append(
                {
                    "id": ids[0].upper(),
                    "date": dates[0] if dates else "",
                    "row": row_text[:200],
                }
            )

        if not filings:
            # Fallback: just track IDs from page
            for fid in fcc_ids:
                filings.append({"id": fid.upper(), "date": "", "row": fid})

        id_set = sorted({f["id"] for f in filings})
        date_map = {f["id"]: f.get("date") or "" for f in filings}
        fingerprint = content_hash("|".join(f"{i}:{date_map.get(i, '')}" for i in id_set))
        parsed = {
            "fcc_ids": id_set,
            "dates": date_map,
            "count": len(id_set),
        }

        alerts = []
        prev = self.state().parsed
        if self.state().baseline_set:
            prev_ids = set(prev.get("fcc_ids") or [])
            new_ids = [i for i in id_set if i not in prev_ids]
            if new_ids:
                alerts.append(
                    self.make_alert(
                        "New Valve FCC filing(s)",
                        f"New FCC ID(s): {', '.join(new_ids)}",
                        url,
                    )
                )
            prev_dates = prev.get("dates") or {}
            changed = []
            for fid, date in date_map.items():
                old = prev_dates.get(fid)
                if old is not None and old != date and date:
                    changed.append(f"{fid}: {old!r} -> {date!r}")
            if changed:
                alerts.append(
                    self.make_alert(
                        "FCC confidentiality/date changed",
                        "; ".join(changed[:5]),
                        url,
                    )
                )

        return WatcherResult(
            watcher_id=self.id,
            success=True,
            fingerprint=fingerprint,
            parsed=parsed,
            alerts=alerts,
            log_message=f"{self.id}: filings={len(id_set)} newest_sample={id_set[:3]}",
            http_status=resp.status,
        )
