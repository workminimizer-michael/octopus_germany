"""Quarter-hour electricity cost calculation for external cost statistics."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .cost_report import (
    _forecast_price,
    _select_product,
    _standing_charge,
    _tariff_rate,
)
from .statistics_reconciliation import QUARTER, complete_hours, utc_datetime
from .tariff import parse_product_datetime

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DEFAULT_VAT_FRACTION = Decimal("0.19")
# A forecast entry is at most one day long; anything longer is ignored.
_MAX_FORECAST_SPAN = timedelta(days=2)
_HOUR = timedelta(hours=1)


def capture_quarter_prices(
    cached: Mapping[str, str],
    products: Sequence[dict[str, Any]],
    retained_start: datetime,
) -> tuple[dict[str, str], int]:
    """
    Merge forecast gross prices (EUR/kWh) per UTC quarter into the price cache.

    The API only publishes prices for today and tomorrow, so prices must be
    captured while they are available. Newer captures replace older values for
    the same quarter. Quarters before ``retained_start`` are dropped.
    """
    merged = {
        key: value
        for key, value in cached.items()
        if utc_datetime(key) >= retained_start
    }
    changed = 0
    for product in products:
        for forecast in product.get("unitRateForecast") or []:
            start = parse_product_datetime(forecast.get("validFrom"))
            end = parse_product_datetime(forecast.get("validTo"))
            if start is None or end is None or not start < end:
                continue
            if end - start > _MAX_FORECAST_SPAN or start.minute % 15 or start.second:
                continue
            at = start
            while at < end:
                if at >= retained_start:
                    selected = _select_product(list(products), at)
                    price = _forecast_price(selected, at) if selected else None
                    if price is not None:
                        key = at.astimezone(UTC).isoformat()
                        if merged.get(key) != str(price):
                            merged[key] = str(price)
                            changed += 1
                at += QUARTER
    return merged, changed


def _quarter_price(
    key: str,
    prices: Mapping[str, str],
    products: Sequence[dict[str, Any]],
    vat: Decimal,
) -> Decimal | None:
    """Return the captured price, or the fixed price of a non-dynamic tariff."""
    if (captured := prices.get(key)) is not None:
        try:
            return Decimal(captured)
        except InvalidOperation:
            return None
    at = utc_datetime(key)
    product = _select_product(list(products), at)
    # Dynamic tariffs are only priced from captured forecasts; never guess.
    if product is None or product.get("isTimeOfUse"):
        return None
    return _tariff_rate(product, at, vat)[1]


def _local_day_hours(day: Any, zone: ZoneInfo) -> int:
    """Return the number of hours in a local calendar day (23/24/25)."""
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    return int((end - start) / _HOUR)


def hourly_costs(  # noqa: PLR0913
    intervals: Mapping[str, Mapping[str, Any]],
    conflicts: set[str],
    prices: Mapping[str, str],
    products: Sequence[dict[str, Any]],
    timezone: str,
    vat: Decimal = DEFAULT_VAT_FRACTION,
) -> tuple[dict[datetime, Decimal], dict[str, int]]:
    """
    Return gross EUR per complete UTC hour and diagnostic counters.

    Each quarter's consumption is multiplied by its own price before summing.
    The daily standing charge is spread evenly across the local day's hours,
    so a fully priced day carries exactly one daily standing charge.
    """
    zone = ZoneInfo(timezone)
    result: dict[datetime, Decimal] = {}
    counts = {"missing_price_hours": 0, "missing_standing_charge_hours": 0}
    standing_by_day: dict[Any, Decimal | None] = {}
    for hour in sorted(complete_hours(dict(intervals), conflicts)):
        keys = [(hour + offset * QUARTER).isoformat() for offset in range(4)]
        quarter_prices = [_quarter_price(key, prices, products, vat) for key in keys]
        if any(price is None for price in quarter_prices):
            counts["missing_price_hours"] += 1
            continue
        energy_cost = sum(
            (
                Decimal(str(intervals[key]["value_kwh"])) * price
                for key, price in zip(keys, quarter_prices, strict=True)
                if price is not None
            ),
            Decimal(0),
        )
        day = hour.astimezone(zone).date()
        if day not in standing_by_day:
            noon = datetime.combine(day, time(12), zone)
            standing_by_day[day] = _standing_charge(
                _select_product(list(products), noon), noon, vat
            )
        standing = standing_by_day[day]
        if standing is None:
            counts["missing_standing_charge_hours"] += 1
            standing = Decimal(0)
        result[hour] = energy_cost + standing / _local_day_hours(day, zone)
    return result, counts
