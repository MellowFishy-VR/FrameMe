"""Optional Discord delivery for FrameMe alerts (webhook or bot token).

Bot mode needs only a bot token + channel ID (no guild ID). The bot must be
invited to the server with Send Messages (and Embed Links) in that channel.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Iterable

from monitor.models import AlertEvent

log = logging.getLogger("frameme.discord")

API_BASE = "https://discord.com/api/v10"

# Embed colors by tier (Discord integer color)
TIER_COLORS = {
    1: 0xE74C3C,  # red — critical
    2: 0xF1C40F,  # yellow
    3: 0x3498DB,  # blue — digest / low urgency
}


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
    ) -> None:
        env_url = (os.environ.get("FRAMEME_DISCORD_WEBHOOK") or "").strip()
        env_token = (os.environ.get("FRAMEME_DISCORD_BOT_TOKEN") or "").strip()
        env_channel = (os.environ.get("FRAMEME_DISCORD_CHANNEL_ID") or "").strip()

        self.webhook_url = (webhook_url or env_url).strip()
        self.bot_token = (bot_token or env_token).strip()
        self.channel_id = str(channel_id or env_channel).strip()
        self.tiers = set(int(t) for t in (tiers if tiers is not None else (1, 2, 3)))
        self.username = username

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

    def _post_webhook(self, payload: dict) -> bool:
        return self._request(
            self.webhook_url,
            payload,
            headers={"Content-Type": "application/json", "User-Agent": "FrameMe/2.0"},
            label="webhook",
        )

    def _post_bot(self, payload: dict) -> bool:
        url = f"{API_BASE}/channels/{self.channel_id}/messages"
        return self._request(
            url,
            payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bot {self.bot_token}",
                "User-Agent": "FrameMe/2.0",
            },
            label="bot",
        )

    def _request(
        self,
        url: str,
        payload: dict,
        *,
        headers: dict[str, str],
        label: str,
    ) -> bool:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
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


# Back-compat alias
DiscordWebhook = DiscordNotifier
