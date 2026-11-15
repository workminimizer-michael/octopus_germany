"""Quarter-hour electricity cost calculation for external cost statistics."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
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


def average_price(  # noqa: PLR0913
    intervals: Mapping[str, Mapping[str, Any]],
    conflicts: set[str],
    prices: Mapping[str, str],
    products: Sequence[dict[str, Any]],
    timezone: str,
    first_day: date,
    last_day: date,
    vat: Decimal = DEFAULT_VAT_FRACTION,
) -> dict[str, Any]:
    """
    Return the consumption-weighted average gross price for local days.

    Only quarters with both consumption and a price are used. The effective
    price additionally spreads the standing charge of every day that has at
    least one priced quarter over the priced consumption.
    """
    zone = ZoneInfo(timezone)
    range_start = datetime.combine(first_day, time.min, zone).astimezone(UTC)
    range_end = datetime.combine(
        last_day + timedelta(days=1), time.min, zone
    ).astimezone(UTC)
    consumption = Decimal(0)
    energy_cost = Decimal(0)
    priced_quarters = 0
    priced_days: set[date] = set()
    for key, row in intervals.items():
        at = utc_datetime(key)
        if key in conflicts or not range_start <= at < range_end:
            continue
        price = _quarter_price(key, prices, products, vat)
        if price is None:
            continue
        kwh = Decimal(str(row["value_kwh"]))
        consumption += kwh
        energy_cost += kwh * price
        priced_quarters += 1
        priced_days.add(at.astimezone(zone).date())

    standing_cost = Decimal(0)
    missing_standing_days = 0
    for day in priced_days:
        noon = datetime.combine(day, time(12), zone)
        standing = _standing_charge(_select_product(list(products), noon), noon, vat)
        if standing is None:
            missing_standing_days += 1
        else:
            standing_cost += standing

    expected_quarters = int((range_end - range_start) / QUARTER)
    total_cost = energy_cost + standing_cost
    return {
        "first_day": first_day.isoformat(),
        "last_day": last_day.isoformat(),
        "average_price_eur_per_kwh": (
            round(float(energy_cost / consumption), 6) if consumption else None
        ),
        "effective_price_eur_per_kwh": (
            round(float(total_cost / consumption), 6) if consumption else None
        ),
        "consumption_kwh": round(float(consumption), 6),
        "energy_cost_eur": round(float(energy_cost), 6),
        "standing_charge_eur": round(float(standing_cost), 6),
        "total_cost_eur": round(float(total_cost), 6),
        "priced_quarters": priced_quarters,
        "expected_quarters": expected_quarters,
        "coverage_percent": round(100 * priced_quarters / expected_quarters, 1),
        "complete": priced_quarters == expected_quarters,
        "priced_days": len(priced_days),
        "missing_standing_charge_days": missing_standing_days,
    }


def average_price_summaries(  # noqa: PLR0913
    intervals: Mapping[str, Mapping[str, Any]],
    conflicts: set[str],
    prices: Mapping[str, str],
    products: Sequence[dict[str, Any]],
    timezone: str,
    now: datetime,
    days: int = 30,
) -> dict[str, dict[str, Any]]:
    """Return average prices for yesterday and the last ``days`` local days."""
    yesterday = now.astimezone(ZoneInfo(timezone)).date() - timedelta(days=1)
    args = (intervals, conflicts, prices, products, timezone)
    return {
        "yesterday": average_price(*args, yesterday, yesterday),
        "last_30_days": average_price(
            *args, yesterday - timedelta(days=days - 1), yesterday
        ),
    }
