"""Keep the Discord bot Online via a Gateway presence connection.

FrameMe delivers alerts over REST; without a Gateway session Discord shows the
bot as Offline. This thread only maintains presence (no event handling).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Any

log = logging.getLogger("frameme.discord.presence")

API_BASE = "https://discord.com/api/v10"
GATEWAY_VERSION = 10


class DiscordPresence:
    """Background Gateway session so the bot stays Online while FrameMe runs."""

    def __init__(
        self,
        bot_token: str,
        *,
        activity_name: str = "Steam Frame",
        status: str = "online",
    ) -> None:
        self.bot_token = (bot_token or "").strip()
        self.activity_name = activity_name
        self.status = status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if not self.bot_token or self.running:
            return
        try:
            import websocket  # noqa: F401  # websocket-client
        except ImportError:
            log.warning(
                "websocket-client not installed — Discord bot will appear Offline "
                "(alerts still work). pip install websocket-client"
            )
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="frameme-discord-presence",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def _gateway_url(self) -> str:
        req = urllib.request.Request(
            f"{API_BASE}/gateway/bot",
            headers={
                "Authorization": f"Bot {self.bot_token}",
                "User-Agent": "FrameMe/2.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        base = str(data.get("url") or "wss://gateway.discord.gg")
        return f"{base}?v={GATEWAY_VERSION}&encoding=json"

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 2.0
            except Exception as exc:
                log.warning("Discord presence disconnected: %s", exc)
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _session(self) -> None:
        import websocket

        url = self._gateway_url()
        ws = websocket.create_connection(
            url,
            timeout=30,
            header=["User-Agent: FrameMe/2.0"],
        )
        seq: int | None = None
        heartbeat_ms = 41250
        last_heartbeat = 0.0
        identified = False

        try:
            while not self._stop.is_set():
                ws.settimeout(1.0)
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    raw = None
                except websocket.WebSocketConnectionClosedException:
                    break

                now = time.monotonic()
                if identified and (now - last_heartbeat) * 1000 >= heartbeat_ms * 0.9:
                    ws.send(json.dumps({"op": 1, "d": seq}))
                    last_heartbeat = now

                if not raw:
                    continue

                msg: dict[str, Any] = json.loads(raw)
                op = msg.get("op")
                if msg.get("s") is not None:
                    seq = int(msg["s"])

                if op == 10:  # Hello
                    heartbeat_ms = int(
                        (msg.get("d") or {}).get("heartbeat_interval") or 41250
                    )
                    identify = {
                        "op": 2,
                        "d": {
                            "token": self.bot_token,
                            "intents": 0,
                            "properties": {
                                "os": "linux",
                                "browser": "FrameMe",
                                "device": "FrameMe",
                            },
                            "presence": {
                                "since": None,
                                "activities": [
                                    {
                                        "name": self.activity_name,
                                        "type": 3,  # Watching
                                    }
                                ],
                                "status": self.status,
                                "afk": False,
                            },
                        },
                    }
                    ws.send(json.dumps(identify))
                    last_heartbeat = time.monotonic()
                elif op == 11:  # Heartbeat ACK
                    pass
                elif op == 0 and msg.get("t") == "READY":
                    identified = True
                    log.info("Discord presence online (%s)", self.activity_name)
                elif op == 7:  # Reconnect
                    break
                elif op == 9:  # Invalid session
                    time.sleep(2)
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass
