"""Shared smart-meter error and backoff primitives for API mixins."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

SMART_METER_ERROR_BACKOFF = timedelta(hours=3)


class SmartMeterFetchError(RuntimeError):
    """Raised when the smart-meter endpoint fails instead of returning no data."""


# Reading directions that are not grid consumption. Everything else
# (CONSUMPTION, IMPORT, missing, ...) counts as consumption.
NON_CONSUMPTION_DIRECTIONS = frozenset({"EXPORT", "FEED_IN", "GENERATION", "INJECTION"})


def is_consumption_reading(reading: Mapping[str, Any]) -> bool:
    """Return True unless the reading is explicitly a feed-in/export interval."""
    direction = str(reading.get("reading_direction") or "").upper()
    return direction not in NON_CONSUMPTION_DIRECTIONS
