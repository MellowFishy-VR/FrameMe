"""Persisted per-watcher state (fingerprints, parsed values, backoff)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _fmt_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


class WatcherState:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self.fingerprint: str | None = data.get("fingerprint")
        self.parsed: dict[str, Any] = dict(data.get("parsed") or {})
        self.last_success_at: datetime | None = _parse_dt(data.get("last_success_at"))
        self.last_error_at: datetime | None = _parse_dt(data.get("last_error_at"))
        self.backoff_until: datetime | None = _parse_dt(data.get("backoff_until"))
        self.disabled_reason: str | None = data.get("disabled_reason")
        self.error_count: int = int(data.get("error_count") or 0)
        self.baseline_set: bool = bool(data.get("baseline_set", False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "parsed": self.parsed,
            "last_success_at": _fmt_dt(self.last_success_at),
            "last_error_at": _fmt_dt(self.last_error_at),
            "backoff_until": _fmt_dt(self.backoff_until),
            "disabled_reason": self.disabled_reason,
            "error_count": self.error_count,
            "baseline_set": self.baseline_set,
        }

    @property
    def is_disabled(self) -> bool:
        return bool(self.disabled_reason)


class StateStore:
    """Thread-safe JSON state files under ~/.config/frameme/state/."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, WatcherState] = {}

    def path_for(self, watcher_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in watcher_id)
        return self.root / f"{safe}.json"

    def get(self, watcher_id: str) -> WatcherState:
        with self._lock:
            if watcher_id in self._cache:
                return self._cache[watcher_id]
            path = self.path_for(watcher_id)
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
            else:
                data = {}
            state = WatcherState(data)
            self._cache[watcher_id] = state
            return state

    def save(self, watcher_id: str, state: WatcherState) -> None:
        with self._lock:
            self._cache[watcher_id] = state
            path = self.path_for(watcher_id)
            path.write_text(
                json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    def mark_success(
        self,
        watcher_id: str,
        fingerprint: str | None,
        parsed: dict[str, Any],
    ) -> WatcherState:
        state = self.get(watcher_id)
        state.fingerprint = fingerprint
        state.parsed = dict(parsed)
        state.last_success_at = datetime.now(timezone.utc)
        state.last_error_at = None
        state.backoff_until = None
        state.error_count = 0
        if not state.baseline_set:
            state.baseline_set = True
        self.save(watcher_id, state)
        return state

    def mark_error(
        self,
        watcher_id: str,
        error: str,
        backoff_until: datetime | None = None,
    ) -> WatcherState:
        state = self.get(watcher_id)
        state.last_error_at = datetime.now(timezone.utc)
        state.error_count += 1
        if backoff_until is not None:
            state.backoff_until = backoff_until
        self.save(watcher_id, state)
        return state

    def disable(self, watcher_id: str, reason: str) -> WatcherState:
        state = self.get(watcher_id)
        state.disabled_reason = reason
        self.save(watcher_id, state)
        return state
