"""Tier 1: Steam store appdetails API for Steam Frame (4165890)."""

from __future__ import annotations

from steam_checker import STEAM_FRAME_APP_ID, STEAM_FRAME_URL, ProductStatus, check_availability

from monitor.models import WatcherResult
from monitor.watchers.base import BaseWatcher


class SteamFrameAppdetailsWatcher(BaseWatcher):
    def poll(self) -> WatcherResult:
        app_id = str(self.config.extra.get("app_id") or STEAM_FRAME_APP_ID)
        result = check_availability(app_id)

        if result.status is ProductStatus.ERROR:
            return WatcherResult(
                watcher_id=self.id,
                success=False,
                error=result.detail or "check failed",
                retryable_error=True,
                log_message=f"{self.id}: ERROR — {result.detail}",
            )

        parsed = {
            "status": result.status.value,
            "product_name": result.product_name,
            "detail": result.detail,
            "purchasable": result.status.is_purchasable,
        }
        fingerprint = result.status.value

        alerts = []
        prev = self.state().parsed
        was_purchasable = bool(prev.get("purchasable"))
        if self.state().baseline_set and result.status.is_purchasable and not was_purchasable:
            title = (
                "Steam Frame is available!"
                if result.status is ProductStatus.AVAILABLE
                else "Steam Frame pre-orders open!"
            )
            alerts.append(
                self.make_alert(
                    title,
                    f"{result.log_line}. Click to reserve.",
                    STEAM_FRAME_URL,
                    stop_monitoring=True,
                )
            )

        return WatcherResult(
            watcher_id=self.id,
            success=True,
            fingerprint=fingerprint,
            parsed=parsed,
            alerts=alerts,
            log_message=f"{self.id}: {result.log_line}",
        )
