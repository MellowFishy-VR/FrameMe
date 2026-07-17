"""Monitor engine: config, scheduler, PICS thread, heartbeat, digest."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitor.config import load_config
from monitor.discord_notify import DiscordNotifier
from monitor.discord_presence import DiscordPresence
from monitor.http_client import HttpClient
from monitor.models import AlertEvent, GlobalConfig, WatcherResult, utcnow
from monitor.pics import PicsWatcherThread, pics_fallback_appdetails_poll
from monitor.scheduler import WatcherScheduler
from monitor.state import StateStore
from monitor.watchers import build_watcher
from monitor.watchers.base import BaseWatcher

log = logging.getLogger("frameme.engine")

LogFn = Callable[[str], None]
AlertFn = Callable[[AlertEvent], None]


class MonitorEngine:
    def __init__(
        self,
        config_path: Path | None = None,
        *,
        on_log: LogFn | None = None,
        on_alert: AlertFn | None = None,
        dry_run: bool = False,
    ) -> None:
        self.config_path = config_path
        self.config: GlobalConfig = load_config(config_path)
        self.store = StateStore(Path(self.config.state_dir))
        self.http = HttpClient(user_agent=self.config.user_agent)
        self.on_log = on_log or (lambda m: log.info(m))
        self._external_on_alert = on_alert
        self.dry_run = dry_run
        dc = self.config.discord
        self.discord = DiscordNotifier(
            enabled=dc.enabled,
            mode=dc.mode,
            webhook_url=dc.webhook_url,
            bot_token=dc.bot_token,
            channel_id=dc.channel_id,
            tiers=dc.tiers,
            username=dc.username,
            send_changes=dc.send_changes,
        )
        self._discord_presence: DiscordPresence | None = None
        if (
            not self.dry_run
            and self.discord.enabled
            and self.discord.transport == "bot"
            and dc.presence
            and self.discord.bot_token
        ):
            self._discord_presence = DiscordPresence(
                self.discord.bot_token,
                activity_name=dc.presence_activity,
            )

        self._watchers: list[BaseWatcher] = []
        self._scheduler: WatcherScheduler | None = None
        self._pics: PicsWatcherThread | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._running = False

        self._tier3_queue: list[AlertEvent] = []
        self._tier3_lock = threading.Lock()
        self._last_digest_at: datetime | None = None
        self._last_heartbeat_alert_at: datetime | None = None
        self._started_at: datetime | None = None

        self._build_watchers()
        if self.discord.enabled:
            extra = ""
            if self.discord.send_changes:
                extra = " + change diffs"
            if self._discord_presence:
                extra += " + presence"
            self.on_log(
                f"Discord {self.discord.transport} enabled "
                f"(tiers={sorted(self.discord.tiers)}{extra})."
            )
        elif dc.enabled:
            self.on_log(
                "Discord enabled in config but not configured "
                "(need webhook_url, or bot_token + channel_id)."
            )

    def _on_change(
        self,
        source: str,
        summary: str,
        *,
        old_text: str = "",
        new_text: str = "",
        url: str = "",
        alert: bool = False,
    ) -> None:
        if self.dry_run or not self.discord.enabled:
            return
        try:
            if self.discord.send_change(
                source,
                summary,
                old_text=old_text,
                new_text=new_text,
                url=url,
                alert=alert,
            ):
                self.on_log(f"Discord change diff sent: {source}")
            elif self.discord.send_changes:
                self.on_log(f"Discord change diff failed: {source}")
        except Exception as exc:
            self.on_log(f"Discord change error: {exc}")

    def _build_watchers(self) -> None:
        self._watchers = []
        for wc in self.config.watchers:
            if not wc.enabled:
                continue
            if wc.id == "steam_pics":
                continue  # handled by PicsWatcherThread
            watcher = build_watcher(wc, self.http, self.store)
            if watcher is None:
                self.on_log(f"Unknown watcher id in config: {wc.id}")
                continue
            watcher.on_change = self._on_change
            self._watchers.append(watcher)

    @property
    def enabled_count(self) -> int:
        count = len(self._watchers)
        for wc in self.config.watchers:
            if wc.enabled and wc.id == "steam_pics":
                count += 1
        return count

    @property
    def running(self) -> bool:
        return self._running

    def _emit_alert(self, alert: AlertEvent) -> None:
        # Heartbeat / digest / PICS alerts that skip BaseWatcher.apply_result
        if alert.source in ("heartbeat", "digest") or alert.source.startswith("steam_pics"):
            try:
                from monitor.changelog import append_change

                append_change(
                    alert.source,
                    f"[T{alert.tier}] {alert.title} — {alert.message}",
                    url=alert.url or "",
                    alert=True,
                )
            except Exception as exc:
                self.on_log(f"changelog write failed: {exc}")

        if self.dry_run:
            self.on_log(f"DRY-RUN alert suppressed: {alert.log_line()}")
            return
        if alert.tier >= 3:
            with self._tier3_lock:
                self._tier3_queue.append(alert)
            self.on_log(f"Queued Tier 3: {alert.log_line()}")
            self._maybe_flush_digest(force=False)
            return
        self._deliver_alert(alert)

    def _deliver_alert(self, alert: AlertEvent) -> None:
        """Desktop notify (+ optional Discord) for a ready-to-fire alert."""
        if self.discord.enabled:
            try:
                if self.discord.send_alert(alert):
                    self.on_log(
                        f"Discord {self.discord.transport} sent: {alert.title}"
                    )
                elif alert.tier in self.discord.tiers:
                    self.on_log(
                        f"Discord {self.discord.transport} failed: {alert.title}"
                    )
            except Exception as exc:
                self.on_log(f"Discord error: {exc}")
        if self._external_on_alert:
            self._external_on_alert(alert)

    def _maybe_flush_digest(self, force: bool) -> None:
        with self._tier3_lock:
            if not self._tier3_queue:
                return
            now = utcnow()
            due_time = (
                self._last_digest_at is None
                or (now - self._last_digest_at).total_seconds()
                >= self.config.tier3_digest_interval_seconds
            )
            due_count = len(self._tier3_queue) >= self.config.tier3_digest_min_items
            if not force and not due_time and not due_count:
                return
            items = list(self._tier3_queue)
            self._tier3_queue.clear()
            self._last_digest_at = now

        lines = [f"• [{a.source}] {a.title}: {a.message}" for a in items]
        digest = AlertEvent(
            source="digest",
            tier=3,
            title=f"FrameMe Tier 3 digest ({len(items)} updates)",
            message="\n".join(lines)[:1500],
            url=items[0].url if items else "",
        )
        if self.dry_run:
            self.on_log(f"DRY-RUN digest suppressed: {digest.log_line()}")
            return
        self._deliver_alert(digest)

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._started_at = utcnow()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="frameme-engine",
            daemon=True,
        )
        self._thread.start()
        if self._discord_presence:
            self._discord_presence.start()
            self.on_log("Discord bot presence starting (keeps bot Online).")
        self.on_log(f"Monitor engine started ({self.enabled_count} watchers).")

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        if self._discord_presence:
            self._discord_presence.stop()
            self._discord_presence = None
        if self._pics:
            self._pics.stop()
        if self._scheduler:
            self._scheduler.stop()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._maybe_flush_digest(force=True)
        try:
            from monitor.browser import shutdown_browser_session

            shutdown_browser_session()
        except Exception:
            pass
        self.on_log("Monitor engine stopped.")

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as exc:
            self.on_log(f"Engine crashed: {exc}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _async_main(self) -> None:
        # Start PICS thread if enabled
        pics_cfg = next(
            (w for w in self.config.watchers if w.id == "steam_pics" and w.enabled),
            None,
        )
        if pics_cfg:
            self._pics = PicsWatcherThread(
                pics_cfg,
                self.store,
                on_alert=self._emit_alert,
                on_log=self.on_log,
                on_change=self._on_change,
            )
            self._pics.start()

        self._scheduler = WatcherScheduler(
            self._watchers,
            stagger_seconds=self.config.stagger_seconds,
            on_log=self.on_log,
            on_alert=self._emit_alert,
        )

        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        digest_task = asyncio.create_task(self._digest_loop())
        sched_task = asyncio.create_task(self._scheduler.run())

        while not self._stop.is_set():
            await asyncio.sleep(0.5)

        self._scheduler.stop()
        if self._pics:
            self._pics.stop()
        for task in (heartbeat_task, digest_task, sched_task):
            task.cancel()
        await asyncio.gather(heartbeat_task, digest_task, sched_task, return_exceptions=True)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._check_heartbeat()
            except Exception as exc:
                self.on_log(f"Heartbeat error: {exc}")
            await asyncio.sleep(60)

    async def _digest_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(30)
            try:
                self._maybe_flush_digest(force=False)
            except Exception as exc:
                self.on_log(f"Digest flush error: {exc}")

    def _tier1_watcher_ids(self) -> list[str]:
        ids = []
        for wc in self.config.watchers:
            if wc.enabled and wc.tier == 1:
                ids.append(wc.id)
        return ids

    def _check_heartbeat(self) -> None:
        timeout = timedelta(seconds=self.config.heartbeat_tier1_timeout_seconds)
        now = utcnow()
        stale: list[str] = []
        for wid in self._tier1_watcher_ids():
            # PICS is tier 2; skip. HTTP watchers use state store.
            if wid == "steam_pics":
                continue
            state = self.store.get(wid)
            if state.is_disabled:
                continue
            last_ok = state.last_success_at or self._started_at
            if last_ok is None:
                continue
            if now - last_ok > timeout:
                stale.append(wid)

        if not stale:
            return

        cooldown = timedelta(seconds=self.config.heartbeat_alert_cooldown_seconds)
        if self._last_heartbeat_alert_at and now - self._last_heartbeat_alert_at < cooldown:
            return

        self._last_heartbeat_alert_at = now
        alert = AlertEvent(
            source="heartbeat",
            tier=1,
            title="MONITORING BROKEN",
            message=(
                "Tier 1 watcher(s) have not successfully polled in "
                f"{self.config.heartbeat_tier1_timeout_seconds // 60} minutes: "
                + ", ".join(stale)
            ),
            url="",
            stop_monitoring=False,
        )
        self.on_log(alert.log_line())
        if not self.dry_run:
            self._deliver_alert(alert)

    def run_test(self) -> int:
        """Run every enabled watcher once; print extracted state; no alerts."""
        self.dry_run = True
        print("=== FrameMe --test (no alerts) ===\n")
        exit_code = 0

        for watcher in self._watchers:
            print(f"--- {watcher.id} (tier {watcher.tier}) ---")
            try:
                result = watcher.poll()
            except Exception as exc:
                result = watcher.handle_http_exception(exc)
                exit_code = 1
            # Persist baseline/state but strip alerts from firing
            fired = watcher.apply_result(result)
            print(f"log: {result.log_message}")
            print(f"success: {result.success}")
            if result.error:
                print(f"error: {result.error}")
                exit_code = 1
            print(f"fingerprint: {result.fingerprint}")
            print(f"parsed: {json.dumps(result.parsed, indent=2, default=str)}")
            print(f"would_alert: {len(fired)} (suppressed in --test)")
            for a in result.alerts:
                print(f"  alert candidate: {a.title} — {a.message}")
            state = self.store.get(watcher.id)
            print(f"stored: {json.dumps(state.to_dict(), indent=2, default=str)}")
            print()

        pics_cfg = next(
            (w for w in self.config.watchers if w.id == "steam_pics" and w.enabled),
            None,
        )
        if pics_cfg:
            print("--- steam_pics (tier 2) ---")
            pics = PicsWatcherThread(
                pics_cfg,
                self.store,
                on_alert=lambda a: None,
                on_log=lambda m: print(f"log: {m}"),
            )
            try:
                out = pics.run_once_test()
                print(f"parsed: {json.dumps(out.get('parsed'), indent=2, default=str)}")
                print(f"would_alert: {len(out.get('alerts') or [])}")
                for a in out.get("alerts") or []:
                    print(f"  alert candidate: {a.title} — {a.message}")
            except Exception as exc:
                print(f"PICS failed: {exc}")
                print("Attempting soft fallback (appdetails for 3990420)...")
                try:
                    fb = pics_fallback_appdetails_poll(self.store, print)
                    print(f"fallback: {json.dumps(fb, indent=2, default=str)}")
                except Exception as fb_exc:
                    print(f"fallback failed: {fb_exc}")
                    exit_code = 1
            print()

        print("=== done ===")
        return exit_code
