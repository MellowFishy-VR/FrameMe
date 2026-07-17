"""Watcher implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from monitor.watchers.base import BaseWatcher
from monitor.watchers.fcc import FccWatcher
from monitor.watchers.importgenius import ImportGeniusWatcher
from monitor.watchers.komodo import KomodoWatcher
from monitor.watchers.steam_frame_appdetails import SteamFrameAppdetailsWatcher
from monitor.watchers.steam_frame_sale import SteamFrameSaleWatcher
from monitor.watchers.steam_news import SteamNewsWatcher
from monitor.watchers.steamworks import SteamworksWatcher
from monitor.watchers.valve_compliance import ValveComplianceWatcher

if TYPE_CHECKING:
    from monitor.http_client import HttpClient
    from monitor.models import WatcherConfig
    from monitor.state import StateStore

WATCHER_CLASSES: dict[str, type[BaseWatcher]] = {
    "steam_frame_appdetails": SteamFrameAppdetailsWatcher,
    "steam_frame_sale": SteamFrameSaleWatcher,
    "komodo": KomodoWatcher,
    "steamworks": SteamworksWatcher,
    "steam_news": SteamNewsWatcher,
    "importgenius": ImportGeniusWatcher,
    "fcc_valve": FccWatcher,
    "valve_compliance": ValveComplianceWatcher,
    # steam_pics is handled separately (gevent thread) in monitor.pics
}


def build_watcher(
    config: WatcherConfig,
    http: HttpClient,
    store: StateStore,
) -> BaseWatcher | None:
    cls = WATCHER_CLASSES.get(config.id)
    if cls is None:
        return None
    return cls(config, http, store)
