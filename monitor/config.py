"""Load YAML monitor configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from monitor.http_client import DEFAULT_UA
from monitor.models import GlobalConfig, WatcherConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


def load_config(path: Path | None = None) -> GlobalConfig:
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise FileNotFoundError(f"Config not found: {config_path}")

    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    watchers = [WatcherConfig.from_dict(w) for w in (raw.get("watchers") or [])]

    state_dir = raw.get("state_dir") or str(Path.home() / ".config" / "frameme" / "state")

    return GlobalConfig(
        user_agent=str(raw.get("user_agent") or DEFAULT_UA),
        heartbeat_tier1_timeout_seconds=int(
            raw.get("heartbeat_tier1_timeout_seconds", 600)
        ),
        heartbeat_alert_cooldown_seconds=int(
            raw.get("heartbeat_alert_cooldown_seconds", 1800)
        ),
        tier3_digest_interval_seconds=int(
            raw.get("tier3_digest_interval_seconds", 1800)
        ),
        tier3_digest_min_items=int(raw.get("tier3_digest_min_items", 3)),
        state_dir=state_dir,
        stagger_seconds=float(raw.get("stagger_seconds", 7.0)),
        watchers=watchers,
    )
