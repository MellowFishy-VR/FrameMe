"""Compatibility shim — use monitor.discord_notify.DiscordNotifier."""

from monitor.discord_notify import DiscordNotifier, DiscordWebhook

__all__ = ["DiscordNotifier", "DiscordWebhook"]
