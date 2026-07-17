"""Shared models for the multi-source monitor."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AlertEvent:
    source: str
    tier: int
    title: str
    message: str
    url: str
    timestamp: datetime = field(default_factory=utcnow)
    stop_monitoring: bool = False

    def log_line(self) -> str:
        return f"[T{self.tier}] {self.source}: {self.title} — {self.message}"


@dataclass
class WatcherResult:
    """Outcome of a single watcher poll."""

    watcher_id: str
    success: bool
    fingerprint: str | None = None
    parsed: dict[str, Any] = field(default_factory=dict)
    alerts: list[AlertEvent] = field(default_factory=list)
    log_message: str = ""
    error: str | None = None
    disable: bool = False
    disable_reason: str = ""
    http_status: int | None = None
    retryable_error: bool = False
    # Optional full-text payloads for changes.log (e.g. sale page HTML text)
    change_old_text: str | None = None
    change_new_text: str | None = None
    change_summary: str = ""
    # If True, fingerprint changed but should not alert (noise/flicker)
    suppress_alerts: bool = False


@dataclass
class WatcherConfig:
    id: str
    enabled: bool
    tier: int
    interval_seconds: int
    url: str = ""
    urls: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WatcherConfig:
        known = {"id", "enabled", "tier", "interval_seconds", "url", "urls"}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(
            id=str(data["id"]),
            enabled=bool(data.get("enabled", True)),
            tier=int(data.get("tier", 1)),
            interval_seconds=int(data.get("interval_seconds", 60)),
            url=str(data.get("url") or ""),
            urls=list(data.get("urls") or []),
            extra=extra,
        )


@dataclass
class DiscordConfig:
    """Optional Discord forwarding for alerts (webhook or bot token)."""

    enabled: bool = False
    # auto | bot | webhook — auto prefers bot when token+channel are set
    mode: str = "auto"
    webhook_url: str = ""
    bot_token: str = ""
    channel_id: str = ""
    # Which alert tiers to forward (3 = Tier 3 digests)
    tiers: list[int] = field(default_factory=lambda: [1, 2, 3])
    username: str = "FrameMe"  # webhook display name only

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DiscordConfig:
        data = data or {}
        tiers_raw = data.get("tiers", [1, 2, 3])
        tiers = [int(t) for t in tiers_raw] if tiers_raw else [1, 2, 3]
        return cls(
            enabled=bool(data.get("enabled", False)),
            mode=str(data.get("mode") or "auto"),
            webhook_url=str(data.get("webhook_url") or ""),
            bot_token=str(data.get("bot_token") or ""),
            channel_id=str(data.get("channel_id") or ""),
            tiers=tiers,
            username=str(data.get("username") or "FrameMe"),
        )


@dataclass
class GlobalConfig:
    user_agent: str
    heartbeat_tier1_timeout_seconds: int = 600
    heartbeat_alert_cooldown_seconds: int = 1800
    tier3_digest_interval_seconds: int = 1800
    tier3_digest_min_items: int = 3
    state_dir: str = ""
    stagger_seconds: float = 7.0
    watchers: list[WatcherConfig] = field(default_factory=list)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
