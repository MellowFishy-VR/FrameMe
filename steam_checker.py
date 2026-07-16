"""Steam hardware availability checker via the public store API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any

STEAM_FRAME_APP_ID = "4165890"
STEAM_MACHINE_APP_ID = "4165910"
STEAM_FRAME_URL = "https://store.steampowered.com/hardware/steamframe"
STEAM_API_BASE = "https://store.steampowered.com/api/appdetails"


class ProductStatus(Enum):
    UNKNOWN = "unknown"
    NOT_AVAILABLE = "not_available"
    PREORDER_AVAILABLE = "preorder_available"
    AVAILABLE = "available"
    ERROR = "error"

    def label(self) -> str:
        return {
            ProductStatus.UNKNOWN: "Unknown",
            ProductStatus.NOT_AVAILABLE: "Not available",
            ProductStatus.PREORDER_AVAILABLE: "Pre-order available",
            ProductStatus.AVAILABLE: "Available now",
            ProductStatus.ERROR: "Check failed",
        }[self]

    @property
    def is_purchasable(self) -> bool:
        return self in (ProductStatus.PREORDER_AVAILABLE, ProductStatus.AVAILABLE)


@dataclass
class CheckResult:
    app_id: str
    product_name: str
    status: ProductStatus
    detail: str = ""

    @property
    def log_line(self) -> str:
        name = self.product_name or f"App {self.app_id}"
        line = f"{name}: {self.status.label()}"
        if self.detail:
            line += f" ({self.detail})"
        return line


def _fetch_app_details(app_id: str, timeout: float = 20.0) -> dict[str, Any]:
    url = f"{STEAM_API_BASE}?appids={app_id}&cc=us&l=english"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "FrameMe/1.0 (+https://github.com/frameme)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_availability(payload: dict[str, Any], app_id: str) -> CheckResult:
    entry = payload.get(app_id)
    if not entry:
        return CheckResult(app_id, "", ProductStatus.ERROR, "missing app entry")

    if not entry.get("success"):
        return CheckResult(app_id, "", ProductStatus.ERROR, "success=false")

    data = entry.get("data") or {}
    product_name = data.get("name") or f"App {app_id}"

    release_date = data.get("release_date") or {}
    coming_soon = bool(release_date.get("coming_soon", True))
    price_overview = data.get("price_overview")
    package_groups = data.get("package_groups") or []

    has_price = price_overview is not None
    has_purchase_options = len(package_groups) > 0

    if not coming_soon and has_price and has_purchase_options:
        status = ProductStatus.AVAILABLE
    elif coming_soon and has_purchase_options:
        status = ProductStatus.PREORDER_AVAILABLE
    else:
        status = ProductStatus.NOT_AVAILABLE

    detail_parts = [
        f"coming_soon={coming_soon}",
        f"price={'yes' if has_price else 'no'}",
        f"packages={'yes' if has_purchase_options else 'no'}",
    ]
    return CheckResult(app_id, product_name, status, ", ".join(detail_parts))


def check_availability(app_id: str) -> CheckResult:
    try:
        payload = _fetch_app_details(app_id)
        return parse_availability(payload, app_id)
    except urllib.error.HTTPError as exc:
        return CheckResult(app_id, "", ProductStatus.ERROR, f"HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return CheckResult(app_id, "", ProductStatus.ERROR, str(exc.reason))
    except (json.JSONDecodeError, TimeoutError, OSError) as exc:
        return CheckResult(app_id, "", ProductStatus.ERROR, str(exc))
