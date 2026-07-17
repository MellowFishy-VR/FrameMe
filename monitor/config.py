"""Load YAML monitor configuration; generate local config.yaml if missing."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from monitor.http_client import DEFAULT_UA
from monitor.models import DiscordConfig, GlobalConfig, WatcherConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.example.yaml"


def ensure_config(path: Path | None = None) -> Path:
    """Return config path, copying config.example.yaml if config.yaml is missing."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if config_path.is_file():
        return config_path
    if not EXAMPLE_CONFIG_PATH.is_file():
        raise FileNotFoundError(
            f"Config not found: {config_path} (and no {EXAMPLE_CONFIG_PATH.name} to copy)"
        )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_CONFIG_PATH, config_path)
    return config_path


def load_config(path: Path | None = None) -> GlobalConfig:
    config_path = ensure_config(path)

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
        discord=DiscordConfig.from_dict(raw.get("discord")),
    )
