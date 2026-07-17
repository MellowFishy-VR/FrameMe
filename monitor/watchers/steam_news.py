"""Tier 1: Official Steam News RSS (keyword-filtered).

Covers the main Steam RDF news feed plus optional extra feeds / ISteamNews
app hubs. Distinct from the Steamworks group watcher (different URL set).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from monitor.http_client import content_hash
from monitor.models import WatcherResult
from monitor.watchers.base import BaseWatcher

RSS_NS = {"rss": "http://purl.org/rss/1.0/"}
DEFAULT_FEEDS = [
    "https://store.steampowered.com/feeds/news.xml",
]
# When Valve attaches hub news to hardware apps, these start returning items.
DEFAULT_NEWS_APP_IDS = [4165890, 4165910]
DEFAULT_KEYWORDS = (
    r"steam\s*frame|steamframe|steam\s*machine|steammachine|"
    r"steam\s*controller|steamcontroller|"
    r"reservation|pre-?order|preorder|"
    r"standalone\s*verified|hardware/steamframe"
)
STEAM_NEWS_API = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"


class SteamNewsWatcher(BaseWatcher):
    def poll(self) -> WatcherResult:
        feeds = list(self.config.urls) or list(
            self.config.extra.get("feeds") or DEFAULT_FEEDS
        )
        if self.config.url and self.config.url not in feeds:
            feeds.insert(0, self.config.url)

        app_ids = [
            int(x)
            for x in (self.config.extra.get("news_app_ids") or DEFAULT_NEWS_APP_IDS)
        ]
        pattern = re.compile(
            str(self.config.extra.get("keywords") or DEFAULT_KEYWORDS),
            re.I,
        )

        posts: list[dict[str, str]] = []
        errors: list[str] = []

        for feed_url in feeds:
            try:
                posts.extend(self._fetch_feed(feed_url))
            except Exception as exc:
                errors.append(f"feed {feed_url}: {exc}")

        for app_id in app_ids:
            try:
                posts.extend(self._fetch_app_news(app_id))
            except Exception as exc:
                errors.append(f"app {app_id}: {exc}")

        posts = self._dedupe(posts)
        if not posts:
            err = "; ".join(errors) if errors else "no news items parsed"
            return WatcherResult(
                watcher_id=self.id,
                success=False,
                error=err,
                retryable_error=True,
                log_message=f"{self.id}: {err}",
            )

        posts.sort(key=lambda p: p.get("id") or "", reverse=True)
        ids = [p.get("id") or p.get("title") or "" for p in posts[:40]]
        newest = posts[0]
        fingerprint = content_hash("|".join(ids))
        parsed = {
            "newest_id": newest.get("id") or newest.get("title"),
            "newest_title": newest.get("title"),
            "newest_link": newest.get("link"),
            "post_ids": ids,
            "count": len(posts),
        }

        alerts = []
        prev_ids = set(self.state().parsed.get("post_ids") or [])
        if self.state().baseline_set:
            for post in posts[:25]:
                pid = post.get("id") or post.get("title") or ""
                if not pid or pid in prev_ids:
                    continue
                blob = f"{post.get('title', '')}\n{post.get('summary', '')}"
                if not pattern.search(blob):
                    continue
                link = post.get("link") or "https://store.steampowered.com/news/"
                alerts.append(
                    self.make_alert(
                        "Steam News matched keywords",
                        f"New post: {post.get('title', '(no title)')}",
                        link,
                        stop_monitoring=False,
                    )
                )

        note = f" errors={len(errors)}" if errors else ""
        return WatcherResult(
            watcher_id=self.id,
            success=True,
            fingerprint=fingerprint,
            parsed=parsed,
            alerts=alerts,
            log_message=(
                f"{self.id}: newest={newest.get('title')!r} posts={len(posts)} "
                f"alerts={len(alerts)}{note}"
            ),
        )

    def _fetch_feed(self, url: str) -> list[dict[str, str]]:
        resp = self.http.fetch(url)
        root = ET.fromstring(resp.body)
        out: list[dict[str, str]] = []
        # RSS 2.0
        for it in root.findall("./channel/item"):
            post = self._rss2_item(it)
            if post:
                out.append(post)
        if out:
            return out
        # RSS 1.0 / RDF (Steam main news feed)
        for it in root.findall("rss:item", RSS_NS):
            post = self._rdf_item(it)
            if post:
                out.append(post)
        return out

    def _rss2_item(self, item: ET.Element) -> dict[str, str] | None:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        desc = (item.findtext("description") or "").strip()
        if not title and not guid:
            return None
        summary = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
        return {
            "id": guid,
            "title": title,
            "link": link,
            "summary": summary[:2000],
        }

    def _rdf_item(self, item: ET.Element) -> dict[str, str] | None:
        title = (item.findtext("rss:title", default="", namespaces=RSS_NS) or "").strip()
        link = (item.findtext("rss:link", default="", namespaces=RSS_NS) or "").strip()
        about = (
            item.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") or link or title
        ).strip()
        desc = (
            item.findtext("rss:description", default="", namespaces=RSS_NS) or ""
        ).strip()
        if not title and not about:
            return None
        summary = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
        return {
            "id": about,
            "title": title,
            "link": link or about,
            "summary": summary[:2000],
        }

    def _fetch_app_news(self, app_id: int) -> list[dict[str, str]]:
        url = STEAM_NEWS_API + "?" + urlencode({"appid": app_id, "count": 15})
        resp = self.http.fetch(url)
        data = json.loads(resp.text)
        items = (data.get("appnews") or {}).get("newsitems") or []
        out: list[dict[str, str]] = []
        for item in items:
            gid = str(item.get("gid") or item.get("url") or item.get("title") or "")
            title = str(item.get("title") or "").strip()
            link = str(item.get("url") or "").strip()
            summary = BeautifulSoup(
                str(item.get("contents") or ""), "html.parser"
            ).get_text(" ", strip=True)
            if not gid and not title:
                continue
            out.append(
                {
                    "id": f"app{app_id}:{gid}",
                    "title": title,
                    "link": link,
                    "summary": summary[:2000],
                }
            )
        return out

    @staticmethod
    def _dedupe(posts: list[dict[str, str]]) -> list[dict[str, str]]:
        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for post in posts:
            key = post.get("id") or post.get("link") or post.get("title") or ""
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(post)
        return unique
