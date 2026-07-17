"""Tier 2: Steam PICS changelist watcher (anonymous login, no SteamDB scrape)."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from monitor.http_client import backoff_until, content_hash
from monitor.models import AlertEvent, WatcherConfig, utcnow
from monitor.state import StateStore

log = logging.getLogger("frameme.pics")

STEAM_FRAME_APP = 4165890
WIRELESS_ADAPTER_APP = 3990420
TRACKED_APPS = {STEAM_FRAME_APP, WIRELESS_ADAPTER_APP}

STEAMDB_LINKS = {
    STEAM_FRAME_APP: (
        "https://steamdb.info/app/4165890/subs/",
        "https://steamdb.info/app/4165890/history/",
    ),
    WIRELESS_ADAPTER_APP: (
        "https://steamdb.info/app/3990420/subs/",
        "https://steamdb.info/app/3990420/history/",
    ),
}

# Steam EResult values we handle specially
ERESULT_TRY_ANOTHER_CM = 48
ERESULT_RATE_LIMIT = 84
ERESULT_LOGIN_THROTTLE = 87


class PicsWatcherThread:
    """Runs ValvePython steam client in an isolated thread with a persistent login."""

    def __init__(
        self,
        config: WatcherConfig,
        store: StateStore,
        on_alert: Callable[[AlertEvent], None],
        on_log: Callable[[str], None],
        on_success: Callable[[], None] | None = None,
        on_change: Callable[..., None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.on_alert = on_alert
        self.on_log = on_log
        self.on_success = on_success
        self.on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tracked_subs: set[int] = set()
        self._client: Any = None

    @property
    def id(self) -> str:
        return self.config.id

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="frameme-pics",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._disconnect()

    def run_once_test(self) -> dict:
        """Synchronous one-shot for --test mode."""
        try:
            return self._poll_once(emit_alerts=False)
        finally:
            self._disconnect()

    def _run(self) -> None:
        interval = max(30, int(self.config.interval_seconds or 60))
        while not self._stop.is_set():
            state = self.store.get(self.id)
            if state.is_disabled:
                self.on_log(f"{self.id}: disabled — {state.disabled_reason}")
                return
            if state.backoff_until and state.backoff_until > utcnow():
                self._stop.wait(5)
                continue
            try:
                self._poll_once(emit_alerts=True)
            except Exception as exc:
                self.on_log(f"{self.id}: error — {exc}")
                self._disconnect()
                until = backoff_until(state.error_count + 1, interval)
                # TryAnotherCM / throttle: prefer a healthier floor
                msg = str(exc)
                if "48" in msg or "TryAnotherCM" in msg or "RateLimit" in msg or "Throttle" in msg:
                    until = backoff_until(max(state.error_count + 2, 3), max(interval, 60))
                self.store.mark_error(self.id, str(exc), until)
            self._stop.wait(interval)
        self._disconnect()

    def _disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            if getattr(client, "logged_on", False):
                client.logout()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass

    def _ensure_client(self) -> Any:
        from steam.client import SteamClient
        from steam.enums import EResult

        client = self._client
        if client is not None and getattr(client, "logged_on", False):
            return client

        # Stale / dead client
        self._disconnect()

        client = SteamClient()
        # Fresh CM list helps with TryAnotherCM
        try:
            client.cm_servers.clear()
        except Exception:
            pass

        result = client.anonymous_login()
        if result == EResult.TryAnotherCM or int(result) == ERESULT_TRY_ANOTHER_CM:
            try:
                client.disconnect()
            except Exception:
                pass
            client = SteamClient()
            result = client.anonymous_login()

        if result != EResult.OK:
            name = getattr(result, "name", str(result))
            raise RuntimeError(f"anonymous_login failed: {result} ({name})")

        self._client = client
        self.on_log(f"{self.id}: anonymous Steam session connected")
        return client

    def _poll_once(self, emit_alerts: bool) -> dict:
        try:
            from steam.client import SteamClient  # noqa: F401 — import check
        except ImportError as exc:
            msg = f"steam[client] not installed: {exc}"
            self.on_log(f"{self.id}: {msg}")
            raise RuntimeError(msg) from exc

        client = self._ensure_client()
        state = self.store.get(self.id)
        last_change = int(state.parsed.get("change_number") or 0)

        if last_change <= 0:
            info = client.get_product_info(apps=list(TRACKED_APPS), timeout=30)
            snapshot = self._snapshot_from_product_info(info)
            fingerprint = content_hash(str(snapshot))
            baseline_was_set = state.baseline_set
            self.store.mark_success(self.id, fingerprint, snapshot)
            if self.on_success:
                self.on_success()
            self.on_log(
                f"{self.id}: baseline changenumber={snapshot.get('change_number')} "
                f"apps={list(snapshot.get('apps', {}).keys())}"
            )
            return {"parsed": snapshot, "alerts": [], "baseline": not baseline_was_set}

        changes = client.get_changes_since(
            last_change,
            app_changes=True,
            package_changes=True,
        )
        current_change = int(getattr(changes, "current_change_number", 0) or last_change)

        app_changes = list(getattr(changes, "app_changes", []) or [])
        pkg_changes = list(getattr(changes, "package_changes", []) or [])

        interesting_apps = [
            a for a in app_changes if int(getattr(a, "appid", 0)) in TRACKED_APPS
        ]

        info = client.get_product_info(apps=list(TRACKED_APPS), timeout=30)
        snapshot = self._snapshot_from_product_info(info)
        snapshot["change_number"] = max(
            current_change, int(snapshot.get("change_number") or 0)
        )

        for app_data in (snapshot.get("apps") or {}).values():
            for sid in app_data.get("subids") or []:
                self._tracked_subs.add(int(sid))

        interesting_pkgs = [
            p
            for p in pkg_changes
            if int(getattr(p, "packageid", 0)) in self._tracked_subs
        ]

        fingerprint = content_hash(str(snapshot))
        prev = dict(state.parsed)
        prev_fp = state.fingerprint
        baseline_was_set = state.baseline_set
        self.store.mark_success(self.id, fingerprint, snapshot)
        if self.on_success:
            self.on_success()

        alerts: list[AlertEvent] = []
        if baseline_was_set:
            alerts = self._diff_alerts(
                prev,
                snapshot,
                interesting_apps=interesting_apps,
                interesting_pkgs=interesting_pkgs,
            )
            # Steam's global change_number ticks constantly — only log when
            # tracked app/package payload actually changed (or we alerted).
            prev_content = {k: v for k, v in prev.items() if k != "change_number"}
            new_content = {
                k: v for k, v in snapshot.items() if k != "change_number"
            }
            if alerts or prev_content != new_content:
                try:
                    import json

                    from monitor.changelog import append_change

                    old_text = json.dumps(
                        prev_content, indent=2, sort_keys=True, default=str
                    )
                    new_text = json.dumps(
                        new_content, indent=2, sort_keys=True, default=str
                    )
                    summary = (
                        f"PICS snapshot changed cn={prev.get('change_number')}->"
                        f"{snapshot.get('change_number')}"
                        + (
                            f" | alerts: {'; '.join(a.title for a in alerts)}"
                            if alerts
                            else ""
                        )
                    )
                    url = STEAMDB_LINKS[STEAM_FRAME_APP][1]
                    append_change(
                        self.id,
                        summary,
                        old_text=old_text,
                        new_text=new_text,
                        url=url,
                        alert=bool(alerts),
                    )
                    if self.on_change:
                        try:
                            self.on_change(
                                self.id,
                                summary,
                                old_text=old_text,
                                new_text=new_text,
                                url=url,
                                alert=bool(alerts),
                            )
                        except Exception:
                            pass
                except Exception:
                    pass

        if emit_alerts:
            for alert in alerts:
                self.on_alert(alert)

        self.on_log(
            f"{self.id}: changenumber={snapshot.get('change_number')} "
            f"app_changes={len(interesting_apps)} pkg_changes={len(interesting_pkgs)} "
            f"alerts={len(alerts)}"
        )
        return {"parsed": snapshot, "alerts": alerts}

    def _snapshot_from_product_info(self, info) -> dict:
        apps_out: dict[str, dict] = {}
        change_number = 0
        apps = getattr(info, "apps", None) or info.get("apps") if isinstance(info, dict) else {}
        if hasattr(info, "apps"):
            apps = info.apps
        elif isinstance(info, dict):
            apps = info.get("apps") or {}

        for appid, appinfo in (apps or {}).items():
            aid = int(appid)
            if aid not in TRACKED_APPS:
                continue
            data = appinfo if isinstance(appinfo, dict) else {}
            common = data.get("common") or {}
            extended = data.get("extended") or {}
            depots = data.get("depots") or {}

            cn = data.get("_change_number") or data.get("change_number") or 0
            try:
                change_number = max(change_number, int(cn))
            except (TypeError, ValueError):
                pass

            packages = []
            for key in ("packages", "packageids"):
                val = depots.get(key) or common.get(key) or extended.get(key) or data.get(key)
                if isinstance(val, dict):
                    packages.extend(int(k) for k in val.keys() if str(k).isdigit())
                elif isinstance(val, (list, tuple)):
                    packages.extend(int(x) for x in val if str(x).isdigit())

            release_state = common.get("releasestate") or common.get("ReleaseState") or ""
            name = common.get("name") or f"App {aid}"

            apps_out[str(aid)] = {
                "name": name,
                "releasestate": str(release_state),
                "subids": sorted(set(packages)),
                "type": common.get("type") or "",
                "is_free": common.get("isfreeapp") or common.get("freeapp") or "",
            }

        packages_out: dict[str, dict] = {}
        pkgs = getattr(info, "packages", None)
        if pkgs is None and isinstance(info, dict):
            pkgs = info.get("packages") or {}
        for pid, pkginfo in (pkgs or {}).items():
            data = pkginfo if isinstance(pkginfo, dict) else {}
            packages_out[str(int(pid))] = {
                "billingtype": str(data.get("billingtype") or data.get("BillingType") or ""),
                "status": str(data.get("status") or ""),
                "price": str(
                    (data.get("price") or data.get("Price") or data.get("packageprice") or "")
                )[:200],
            }
            try:
                self._tracked_subs.add(int(pid))
            except (TypeError, ValueError):
                pass

        return {
            "change_number": change_number,
            "apps": apps_out,
            "packages": packages_out,
            "tracked_apps": sorted(TRACKED_APPS),
        }

    def _diff_alerts(
        self,
        prev: dict,
        curr: dict,
        interesting_apps: list,
        interesting_pkgs: list,
    ) -> list[AlertEvent]:
        alerts: list[AlertEvent] = []
        prev_cn = int(prev.get("change_number") or 0)
        curr_cn = int(curr.get("change_number") or 0)

        prev_apps = prev.get("apps") or {}
        curr_apps = curr.get("apps") or {}

        for appid_s, cur in curr_apps.items():
            appid = int(appid_s)
            old = prev_apps.get(appid_s) or {}
            links = STEAMDB_LINKS.get(appid, ("", ""))
            view = f"View: {links[0]} | {links[1]}"

            old_subs = set(old.get("subids") or [])
            new_subs = set(cur.get("subids") or [])
            added = sorted(new_subs - old_subs)
            if added:
                alerts.append(
                    AlertEvent(
                        source=self.id,
                        tier=self.config.tier,
                        title=f"PICS: new sub(s) for app {appid}",
                        message=f"New subids {added} on {cur.get('name')}. {view}",
                        url=links[0] or links[1],
                    )
                )

            if old.get("releasestate") and old.get("releasestate") != cur.get("releasestate"):
                alerts.append(
                    AlertEvent(
                        source=self.id,
                        tier=self.config.tier,
                        title=f"PICS: release state changed for {appid}",
                        message=(
                            f"{old.get('releasestate')!r} -> {cur.get('releasestate')!r}. {view}"
                        ),
                        url=links[1] or links[0],
                    )
                )

        prev_pkgs = prev.get("packages") or {}
        curr_pkgs = curr.get("packages") or {}
        for pid, cur in curr_pkgs.items():
            old = prev_pkgs.get(pid) or {}
            if cur.get("price") and cur.get("price") != old.get("price"):
                app_hint = next(
                    (
                        a
                        for a, d in curr_apps.items()
                        if int(pid) in (d.get("subids") or [])
                    ),
                    None,
                )
                links = STEAMDB_LINKS.get(int(app_hint or STEAM_FRAME_APP), ("", ""))
                alerts.append(
                    AlertEvent(
                        source=self.id,
                        tier=self.config.tier,
                        title=f"PICS: price info for package {pid}",
                        message=f"{old.get('price')!r} -> {cur.get('price')!r}. View: {links[0]}",
                        url=links[0] or links[1],
                    )
                )

        if curr_cn > prev_cn and (interesting_apps or interesting_pkgs or not alerts):
            changed_sections = []
            if interesting_apps:
                changed_sections.append(
                    "apps=" + ",".join(str(getattr(a, "appid", "?")) for a in interesting_apps)
                )
            if interesting_pkgs:
                changed_sections.append(
                    "packages="
                    + ",".join(str(getattr(p, "packageid", "?")) for p in interesting_pkgs)
                )
            if changed_sections or curr_apps != prev_apps:
                if not any("new sub" in a.title.lower() for a in alerts):
                    if interesting_apps or interesting_pkgs or curr_apps != prev_apps:
                        links = STEAMDB_LINKS[STEAM_FRAME_APP]
                        alerts.append(
                            AlertEvent(
                                source=self.id,
                                tier=self.config.tier,
                                title="PICS: tracked app changenumber advanced",
                                message=(
                                    f"{prev_cn} -> {curr_cn}. "
                                    f"sections: {'; '.join(changed_sections) or 'app snapshot'}. "
                                    f"View: {links[0]} | {links[1]}"
                                ),
                                url=links[1],
                            )
                        )

        return alerts


def pics_fallback_appdetails_poll(store: StateStore, on_log: Callable[[str], None]) -> dict:
    """Soft fallback: poll store appdetails for wireless adapter if PICS unavailable."""
    from steam_checker import check_availability

    app_id = str(WIRELESS_ADAPTER_APP)
    result = check_availability(app_id)
    parsed = {
        "fallback": True,
        "status": result.status.value,
        "detail": result.detail,
        "product_name": result.product_name,
    }
    fingerprint = content_hash(str(parsed))
    state = store.get("steam_pics_fallback")
    baseline = state.baseline_set
    store.mark_success("steam_pics_fallback", fingerprint, parsed)
    on_log(f"steam_pics_fallback: {result.log_line}")
    alerts = []
    if baseline and state.parsed.get("status") != parsed["status"]:
        links = STEAMDB_LINKS[WIRELESS_ADAPTER_APP]
        alerts.append(
            AlertEvent(
                source="steam_pics_fallback",
                tier=2,
                title="Wireless adapter appdetails status changed (PICS fallback)",
                message=f"{state.parsed.get('status')} -> {parsed['status']}. View: {links[0]}",
                url=links[0],
            )
        )
    return {"parsed": parsed, "alerts": alerts}
