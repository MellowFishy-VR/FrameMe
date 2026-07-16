"""HTTP helpers: UA, robots.txt, cache-bust, backoff-aware fetch."""

from __future__ import annotations

import hashlib
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.robotparser import RobotFileParser


DEFAULT_UA = (
    "FrameMe/2.0 (+https://github.com/frameme; personal Steam Frame availability monitor)"
)


@dataclass
class FetchResponse:
    url: str
    final_url: str
    status: int
    body: bytes
    headers: dict[str, str]
    not_modified: bool = False

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str) -> str | None:
        lower = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lower:
                return value
        return None


class RobotsCache:
    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._parsers:
            rp = RobotFileParser()
            robots_url = urllib.parse.urljoin(origin, "/robots.txt")
            try:
                rp.set_url(robots_url)
                rp.read()
                self._parsers[origin] = rp
            except Exception:
                # If robots.txt cannot be fetched, allow and let HTTP handle blocks.
                self._parsers[origin] = None
                return True
        rp = self._parsers[origin]
        if rp is None:
            return True
        try:
            return bool(rp.can_fetch(self.user_agent, url))
        except Exception:
            return True


class HttpClient:
    def __init__(
        self,
        user_agent: str = DEFAULT_UA,
        timeout: float = 25.0,
        respect_robots: bool = True,
        steam_hosts_skip_robots: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.steam_hosts_skip_robots = steam_hosts_skip_robots
        self.robots = RobotsCache(user_agent)

    def _is_steam_host(self, url: str) -> bool:
        host = urllib.parse.urlparse(url).netloc.lower()
        return any(
            host.endswith(h)
            for h in (
                "steampowered.com",
                "steamcommunity.com",
                "steamstatic.com",
            )
        )

    def should_check_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return False
        if self.steam_hosts_skip_robots and self._is_steam_host(url):
            return False
        return True

    def cache_bust(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        qs["_frameme"] = [str(int(time.time()))]
        new_query = urllib.parse.urlencode(qs, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))

    def fetch(
        self,
        url: str,
        *,
        cache_bust: bool = False,
        etag: str | None = None,
        last_modified: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        if self.should_check_robots(url) and not self.robots.allowed(url):
            raise PermissionError(f"robots.txt disallows fetch: {url}")

        target = self.cache_bust(url) if cache_bust else url
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if extra_headers:
            headers.update(extra_headers)

        request = urllib.request.Request(target, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = resp.read()
                status = getattr(resp, "status", 200) or 200
                hdrs = {k: v for k, v in resp.headers.items()}
                return FetchResponse(
                    url=url,
                    final_url=resp.geturl(),
                    status=int(status),
                    body=body,
                    headers=hdrs,
                )
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            hdrs = {k: v for k, v in (exc.headers.items() if exc.headers else [])}
            if exc.code == 304:
                return FetchResponse(
                    url=url,
                    final_url=url,
                    status=304,
                    body=b"",
                    headers=hdrs,
                    not_modified=True,
                )
            raise HttpError(exc.code, str(exc.reason), body, hdrs) from exc


class HttpError(Exception):
    def __init__(
        self,
        status: int,
        reason: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"HTTP {status}: {reason}")
        self.status = status
        self.reason = reason
        self.body = body
        self.headers = headers or {}

    @property
    def retryable(self) -> bool:
        return self.status in (429, 403) or 500 <= self.status <= 599


def compute_backoff_seconds(error_count: int, base_interval: int) -> float:
    """Exponential backoff with jitter; never shorter than base interval."""
    exp = min(base_interval * (2 ** max(0, error_count - 1)), 3600 * 6)
    delay = max(float(base_interval), float(exp))
    jitter = delay * 0.2 * random.random()
    return delay + jitter


def backoff_until(error_count: int, base_interval: int) -> datetime:
    seconds = compute_backoff_seconds(error_count, base_interval)
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def short_diff(old: str, new: str, limit: int = 280) -> str:
    """Cheap line-oriented diff snippet for alerts."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    old_set = set(old_lines)
    new_set = set(new_lines)
    added = [ln for ln in new_lines if ln not in old_set][:8]
    removed = [ln for ln in old_lines if ln not in new_set][:8]
    parts: list[str] = []
    for ln in added:
        parts.append(f"+ {ln.strip()[:120]}")
    for ln in removed:
        parts.append(f"- {ln.strip()[:120]}")
    if not parts:
        return "(content changed)"
    snippet = " | ".join(parts)
    return snippet[:limit]
