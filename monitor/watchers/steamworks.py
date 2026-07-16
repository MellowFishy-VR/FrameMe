"""Tier 1: Steamworks group announcements (keyword match)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from monitor.http_client import content_hash
from monitor.models import WatcherResult
from monitor.watchers.base import BaseWatcher

KEYWORD_RE = re.compile(r"frame|reservation|pre-?order|headset", re.I)
RSS_URL = "https://steamcommunity.com/groups/steamworks/rss/"
PAGE_URL = "https://steamcommunity.com/groups/steamworks/announcements"


class SteamworksWatcher(BaseWatcher):
    def poll(self) -> WatcherResult:
        url = self.config.url or PAGE_URL
        posts: list[dict[str, str]] = []
        source = "rss"

        try:
            posts = self._fetch_rss()
        except Exception:
            source = "html"
            try:
                posts = self._fetch_html(url)
            except Exception as exc:
                return self.handle_http_exception(exc)

        if not posts:
            return WatcherResult(
                watcher_id=self.id,
                success=False,
                error="no announcements parsed",
                retryable_error=True,
                log_message=f"{self.id}: no announcements parsed ({source})",
            )

        # Track newest post id/title set
        newest = posts[0]
        ids = [p.get("id") or p.get("title") or "" for p in posts[:20]]
        fingerprint = content_hash("|".join(ids))
        parsed = {
            "newest_id": newest.get("id") or newest.get("title"),
            "newest_title": newest.get("title"),
            "newest_link": newest.get("link"),
            "post_ids": ids,
        }

        alerts = []
        prev_ids = set(self.state().parsed.get("post_ids") or [])
        if self.state().baseline_set:
            for post in posts[:10]:
                pid = post.get("id") or post.get("title") or ""
                if pid in prev_ids:
                    continue
                blob = f"{post.get('title', '')}\n{post.get('summary', '')}"
                if KEYWORD_RE.search(blob):
                    link = post.get("link") or url
                    alerts.append(
                        self.make_alert(
                            "Steamworks announcement matched keywords",
                            f"New post: {post.get('title', '(no title)')}",
                            link,
                            stop_monitoring=False,
                        )
                    )

        return WatcherResult(
            watcher_id=self.id,
            success=True,
            fingerprint=fingerprint,
            parsed=parsed,
            alerts=alerts,
            log_message=(
                f"{self.id}: newest={newest.get('title')!r} posts={len(posts)} via={source}"
            ),
        )

    def _fetch_rss(self) -> list[dict[str, str]]:
        resp = self.http.fetch(RSS_URL)
        root = ET.fromstring(resp.body)
        posts: list[dict[str, str]] = []
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link or title).strip()
            desc = (item.findtext("description") or "").strip()
            # Strip HTML from description
            summary = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
            posts.append(
                {
                    "id": guid,
                    "title": title,
                    "link": link,
                    "summary": summary[:2000],
                }
            )
        return posts

    def _fetch_html(self, url: str) -> list[dict[str, str]]:
        resp = self.http.fetch(url)
        soup = BeautifulSoup(resp.text, "html.parser")
        posts: list[dict[str, str]] = []
        for block in soup.select(".announcement, .bodytext, .announce_summary, a"):
            # Prefer announcement list items
            pass
        for a in soup.select("a[href*='announcements/detail'], a[href*='/announcements/']"):
            href = a.get("href") or ""
            title = a.get_text(" ", strip=True)
            if not title or len(title) < 4:
                continue
            if not href.startswith("http"):
                href = "https://steamcommunity.com" + href
            posts.append(
                {
                    "id": href,
                    "title": title,
                    "link": href,
                    "summary": "",
                }
            )
        # Dedupe by id
        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for p in posts:
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            unique.append(p)
        return unique
