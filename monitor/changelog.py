"""Append-only change log with unified diffs under ~/.config/frameme/."""

from __future__ import annotations

import difflib
import threading
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()
DEFAULT_LOG = Path.home() / ".config" / "frameme" / "changes.log"


def change_log_path() -> Path:
    path = DEFAULT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def unified_diff(old_text: str, new_text: str, *, context: int = 3) -> str:
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
            n=context,
        )
    )
    return "\n".join(lines) if lines else "(no line-level diff — whitespace/hash only)"


def append_change(
    source: str,
    summary: str,
    *,
    old_text: str | None = None,
    new_text: str | None = None,
    url: str = "",
    alert: bool = False,
) -> Path:
    """Write a change entry. Returns the log file path."""
    path = change_log_path()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        "=" * 72,
        f"[{stamp}] source={source} alert={'yes' if alert else 'no'}",
        f"summary: {summary}",
    ]
    if url:
        parts.append(f"url: {url}")
    if old_text is not None and new_text is not None:
        parts.append(f"size: {len(old_text)} -> {len(new_text)} chars")
        parts.append("--- diff ---")
        parts.append(unified_diff(old_text, new_text))
    parts.append("")

    with _lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(parts) + "\n")
    return path
