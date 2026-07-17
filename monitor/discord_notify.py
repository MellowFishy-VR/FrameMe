"""Optional Discord delivery for FrameMe alerts and change diffs.

Bot mode needs only a bot token + channel ID (no guild ID). The bot must be
invited to the server with Send Messages, Embed Links, and Attach Files.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
import urllib.error
import urllib.request
from typing import Iterable

from monitor.changelog import unified_diff
from monitor.models import AlertEvent

log = logging.getLogger("frameme.discord")

API_BASE = "https://discord.com/api/v10"

# Embed colors by tier (Discord integer color)
TIER_COLORS = {
    1: 0xE74C3C,  # red — critical
    2: 0xF1C40F,  # yellow
    3: 0x3498DB,  # blue — digest / low urgency
}
CHANGE_COLOR = 0x2ECC71  # green — content change
CHANGE_ALERT_COLOR = 0xE67E22  # orange — change that also alerted

# Discord embed description limit is 4096; leave room for fences/headers.
EMBED_DIFF_LIMIT = 3500
# Prefer attaching a .diff when longer than this.
ATTACH_DIFF_THRESHOLD = 2800


class DiscordNotifier:
    def __init__(
        self,
        *,
        enabled: bool,
        mode: str = "auto",
        webhook_url: str = "",
        bot_token: str = "",
        channel_id: str = "",
        tiers: Iterable[int] | None = None,
        username: str = "FrameMe",
        send_changes: bool = True,
    ) -> None:
        env_url = (os.environ.get("FRAMEME_DISCORD_WEBHOOK") or "").strip()
        env_token = (os.environ.get("FRAMEME_DISCORD_BOT_TOKEN") or "").strip()
        env_channel = (os.environ.get("FRAMEME_DISCORD_CHANNEL_ID") or "").strip()

        self.webhook_url = (webhook_url or env_url).strip()
        self.bot_token = (bot_token or env_token).strip()
        self.channel_id = str(channel_id or env_channel).strip()
        self.tiers = set(int(t) for t in (tiers if tiers is not None else (1, 2, 3)))
        self.username = username
        self.send_changes = bool(send_changes)

        mode_l = (mode or "auto").strip().lower()
        if mode_l not in ("auto", "bot", "webhook"):
            mode_l = "auto"

        bot_ready = bool(self.bot_token and self.channel_id)
        webhook_ready = bool(self.webhook_url)

        if mode_l == "bot":
            self.transport = "bot" if bot_ready else None
        elif mode_l == "webhook":
            self.transport = "webhook" if webhook_ready else None
        else:
            # Prefer bot when both are configured (token is not a public URL)
            if bot_ready:
                self.transport = "bot"
            elif webhook_ready:
                self.transport = "webhook"
            else:
                self.transport = None

        self.enabled = bool(enabled and self.transport)

    def send_alert(self, alert: AlertEvent) -> bool:
        if not self.enabled or alert.tier not in self.tiers:
            return False
        embed = {
            "title": alert.title[:256],
            "description": (alert.message or "")[:4000],
            "color": TIER_COLORS.get(alert.tier, 0x95A5A6),
            "footer": {"text": f"FrameMe · {alert.source} · Tier {alert.tier}"},
        }
        if alert.url:
            embed["url"] = alert.url

        if self.transport == "bot":
            return self._post_bot({"embeds": [embed]})
        return self._post_webhook({"username": self.username, "embeds": [embed]})

    def send_change(
        self,
        source: str,
        summary: str,
        *,
        old_text: str = "",
        new_text: str = "",
        url: str = "",
        alert: bool = False,
    ) -> bool:
        """Post a unified diff for a substantive watcher change."""
        if not self.enabled or not self.send_changes:
            return False

        diff = unified_diff(old_text or "", new_text or "")
        size_line = f"{len(old_text or '')} → {len(new_text or '')} chars"
        title = f"Change: {source}"[:256]
        header = f"**{summary[:500]}**\n`{size_line}`"
        color = CHANGE_ALERT_COLOR if alert else CHANGE_COLOR
        footer = f"FrameMe · {source}" + (" · alert" if alert else "")

        attach = len(diff) > ATTACH_DIFF_THRESHOLD
        if attach:
            description = (
                f"{header}\n\nFull unified diff attached as "
                f"`{source}.diff` ({len(diff)} chars)."
            )[:4000]
            embed: dict = {
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": footer},
            }
            if url:
                embed["url"] = url
            filename = f"{_safe_filename(source)}.diff"
            return self._post_with_file(
                {"username": self.username, "embeds": [embed]},
                filename,
                diff.encode("utf-8"),
            )

        # Inline diff in a code block (truncate if needed)
        body = diff
        fence = "```diff\n"
        closer = "\n```"
        budget = EMBED_DIFF_LIMIT - len(header) - len(fence) - len(closer) - 20
        truncated = False
        if len(body) > budget:
            body = body[: max(0, budget - 40)] + "\n... [diff truncated] ..."
            truncated = True
        description = f"{header}\n\n{fence}{body}{closer}"
        if truncated:
            description += "\n_(truncated — see `~/.config/frameme/changes.log`)_"
        embed = {
            "title": title,
            "description": description[:4090],
            "color": color,
            "footer": {"text": footer},
        }
        if url:
            embed["url"] = url

        payload = {"username": self.username, "embeds": [embed]}
        if self.transport == "bot":
            # Bot create-message ignores username; harmless to omit
            return self._post_bot({"embeds": [embed]})
        return self._post_webhook(payload)

    def _post_webhook(self, payload: dict) -> bool:
        return self._request_json(
            self.webhook_url,
            payload,
            headers={"Content-Type": "application/json", "User-Agent": "FrameMe/2.0"},
            label="webhook",
        )

    def _post_bot(self, payload: dict) -> bool:
        url = f"{API_BASE}/channels/{self.channel_id}/messages"
        return self._request_json(
            url,
            payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bot {self.bot_token}",
                "User-Agent": "FrameMe/2.0",
            },
            label="bot",
        )

    def _post_with_file(self, payload: dict, filename: str, file_bytes: bytes) -> bool:
        """Multipart upload (bot or webhook) with an attached .diff file."""
        if self.transport == "bot":
            url = f"{API_BASE}/channels/{self.channel_id}/messages"
            # Bot messages don't use webhook username
            body_payload = {"embeds": payload.get("embeds") or []}
            auth = {"Authorization": f"Bot {self.bot_token}"}
        else:
            url = self.webhook_url
            body_payload = payload
            auth = {}

        boundary = f"----FrameMe{uuid.uuid4().hex}"
        json_part = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
        chunks: list[bytes] = []
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            b'Content-Disposition: form-data; name="payload_json"\r\n'
            b"Content-Type: application/json\r\n\r\n"
        )
        chunks.append(json_part)
        chunks.append(b"\r\n")
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="files[0]"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            ).encode()
        )
        chunks.append(file_bytes)
        chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        body = b"".join(chunks)

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "FrameMe/2.0",
            **auth,
        }
        return self._request_raw(url, body, headers=headers, label=f"{self.transport}-file")

    def _request_json(
        self,
        url: str,
        payload: dict,
        *,
        headers: dict[str, str],
        label: str,
    ) -> bool:
        return self._request_raw(
            url, json.dumps(payload).encode("utf-8"), headers=headers, label=label
        )

    def _request_raw(
        self,
        url: str,
        body: bytes,
        *,
        headers: dict[str, str],
        label: str,
    ) -> bool:
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = getattr(resp, "status", 200) or 200
                if 200 <= int(status) < 300:
                    return True
                log.warning("Discord %s unexpected status %s", label, status)
                return False
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            log.warning("Discord %s HTTP %s: %s", label, exc.code, detail)
            return False
        except Exception as exc:
            log.warning("Discord %s failed: %s", label, exc)
            return False


def _safe_filename(source: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in source)
    return (cleaned or "change")[:64]


# Back-compat alias
DiscordWebhook = DiscordNotifier
