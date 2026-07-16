"""Staggered concurrent watcher scheduling on an asyncio loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from monitor.models import AlertEvent, WatcherResult
from monitor.watchers.base import BaseWatcher

log = logging.getLogger("frameme.scheduler")

LogFn = Callable[[str], None]
AlertFn = Callable[[AlertEvent], None]
ResultFn = Callable[[str, WatcherResult, list[AlertEvent]], None]


class WatcherScheduler:
    def __init__(
        self,
        watchers: list[BaseWatcher],
        *,
        stagger_seconds: float = 7.0,
        on_log: LogFn | None = None,
        on_alert: AlertFn | None = None,
        on_result: ResultFn | None = None,
    ) -> None:
        self.watchers = watchers
        self.stagger_seconds = stagger_seconds
        self.on_log = on_log or (lambda _m: None)
        self.on_alert = on_alert or (lambda _a: None)
        self.on_result = on_result or (lambda _i, _r, _a: None)
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    async def run(self) -> None:
        self._stopping = False
        for index, watcher in enumerate(self.watchers):
            delay = index * self.stagger_seconds
            task = asyncio.create_task(
                self._watcher_loop(watcher, delay),
                name=f"watcher-{watcher.id}",
            )
            self._tasks.append(task)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()

    async def run_once(self) -> list[tuple[str, WatcherResult, list[AlertEvent]]]:
        results = []
        for watcher in self.watchers:
            item = await asyncio.to_thread(self._poll_one, watcher)
            results.append(item)
        return results

    def _poll_one(
        self, watcher: BaseWatcher
    ) -> tuple[str, WatcherResult, list[AlertEvent]]:
        try:
            result = watcher.poll()
        except Exception as exc:
            result = watcher.handle_http_exception(exc)
            result.log_message = result.log_message or f"{watcher.id}: crash — {exc}"
        alerts = watcher.apply_result(result)
        if result.log_message:
            self.on_log(result.log_message)
        for alert in alerts:
            self.on_alert(alert)
        self.on_result(watcher.id, result, alerts)
        return watcher.id, result, alerts

    async def _watcher_loop(self, watcher: BaseWatcher, initial_delay: float) -> None:
        if initial_delay > 0:
            try:
                await asyncio.sleep(initial_delay)
            except asyncio.CancelledError:
                return

        while not self._stopping:
            state = watcher.state()
            if state.is_disabled:
                self.on_log(f"{watcher.id}: skipped (disabled)")
                return

            now = datetime.now(timezone.utc)
            if state.backoff_until and state.backoff_until > now:
                wait = (state.backoff_until - now).total_seconds()
                self.on_log(f"{watcher.id}: backing off for {wait:.0f}s")
                try:
                    await asyncio.sleep(max(1.0, wait))
                except asyncio.CancelledError:
                    return
                continue

            await asyncio.to_thread(self._poll_one, watcher)

            try:
                await asyncio.sleep(max(1, watcher.interval))
            except asyncio.CancelledError:
                return
