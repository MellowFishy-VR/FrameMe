"""Base watcher with transition-only alerting helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from monitor.http_client import HttpClient, HttpError, backoff_until
from monitor.models import AlertEvent, WatcherConfig, WatcherResult, utcnow
from monitor.state import StateStore, WatcherState


class BaseWatcher(ABC):
    id: str = ""

    def __init__(
        self,
        config: WatcherConfig,
        http: HttpClient,
        store: StateStore,
    ) -> None:
        self.config = config
        self.http = http
        self.store = store
        self.id = config.id

    @property
    def tier(self) -> int:
        return self.config.tier

    @property
    def interval(self) -> int:
        return self.config.interval_seconds

    def state(self) -> WatcherState:
        return self.store.get(self.id)

    def is_due(self) -> bool:
        state = self.state()
        if state.is_disabled:
            return False
        if state.backoff_until and state.backoff_until > utcnow():
            return False
        return True

    @abstractmethod
    def poll(self) -> WatcherResult:
        """Perform one check. Must not raise for expected network failures."""

    def apply_result(self, result: WatcherResult) -> list[AlertEvent]:
        """Persist state and return alerts that should actually fire."""
        if result.disable:
            self.store.disable(self.id, result.disable_reason or "disabled")
            return []

        if not result.success:
            until = None
            if result.retryable_error:
                state = self.state()
                until = backoff_until(state.error_count + 1, self.interval)
            self.store.mark_error(self.id, result.error or "error", until)
            return []

        state = self.state()
        previous_fp = state.fingerprint
        previous_parsed = dict(state.parsed)
        baseline_was_set = state.baseline_set

        self.store.mark_success(self.id, result.fingerprint, result.parsed)

        # First successful poll seeds baseline — never alert on absolute state.
        if not baseline_was_set:
            return []

        if result.fingerprint == previous_fp and result.parsed == previous_parsed:
            return []

        return list(result.alerts)

    def make_alert(
        self,
        title: str,
        message: str,
        url: str,
        *,
        stop_monitoring: bool = False,
    ) -> AlertEvent:
        return AlertEvent(
            source=self.id,
            tier=self.tier,
            title=title,
            message=message,
            url=url,
            stop_monitoring=stop_monitoring,
        )

    def handle_http_exception(self, exc: Exception) -> WatcherResult:
        if isinstance(exc, PermissionError):
            return WatcherResult(
                watcher_id=self.id,
                success=False,
                error=str(exc),
                disable=True,
                disable_reason=str(exc),
                log_message=f"{self.id}: disabled — {exc}",
            )
        if isinstance(exc, HttpError):
            return WatcherResult(
                watcher_id=self.id,
                success=False,
                error=str(exc),
                http_status=exc.status,
                retryable_error=exc.retryable,
                log_message=f"{self.id}: HTTP error — {exc}",
            )
        return WatcherResult(
            watcher_id=self.id,
            success=False,
            error=str(exc),
            retryable_error=True,
            log_message=f"{self.id}: error — {exc}",
        )

    def describe_change(self, old: Any, new: Any) -> str:
        return f"{old!r} -> {new!r}"
