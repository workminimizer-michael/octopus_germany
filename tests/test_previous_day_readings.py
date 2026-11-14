"""Tests for previous-day raw smart-meter readings used by the accumulative sensor."""

import asyncio
import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import homeassistant  # noqa: F401

from custom_components.octopus_germany.api import accounts
from custom_components.octopus_germany.api.accounts import (
    AccountApiMixin,
    _extract_malo_number,
    _local_day_bounds,
)
from custom_components.octopus_germany.models import TariffCapabilities
from custom_components.octopus_germany.octopus_germany import OctopusGermany

BERLIN = ZoneInfo("Europe/Berlin")


def _intervals(day: date, count: int, value: str = "0.010000000000000000"):
    start = datetime.combine(day, datetime.min.time(), tzinfo=BERLIN)
    return [
        {
            "start_time": (start + timedelta(minutes=15 * i)).isoformat(),
            "end_time": (start + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": value,
            "unit": "kwh",
            "reading_direction": "CONSUMPTION",
        }
        for i in range(count)
    ]


class _Client(AccountApiMixin):
    def __init__(self, responses):
        self.fetch_electricity_measurements_range = AsyncMock(side_effect=responses)


class PreviousDayRawReadingsTests(unittest.TestCase):
    """Raw 15-minute readings are preferred over legacy hourly queries."""

    def setUp(self):
        fixed_now = datetime(2026, 11, 14, 10, 0, tzinfo=BERLIN)
        patcher = patch.object(accounts, "datetime", wraps=datetime)
        mocked = patcher.start()
        mocked.now = lambda tz=None: fixed_now.astimezone(tz) if tz else fixed_now
        self.addCleanup(patcher.stop)

    def test_extract_malo_number(self):
        self.assertEqual(
            _extract_malo_number({"electricityMalos": [{"maloNumber": "123"}]}), "123"
        )
        self.assertIsNone(_extract_malo_number({"electricityMalos": []}))

    def test_local_day_bounds_match_berlin_midnight(self):
        start, end = _local_day_bounds(date(2026, 11, 13), BERLIN)
        self.assertEqual(start, datetime(2026, 11, 12, 23, 0, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 11, 13, 23, 0, tzinfo=UTC))
        # DST change day has 25 hours.
        start, end = _local_day_bounds(date(2026, 10, 25), BERLIN)
        self.assertEqual(end - start, timedelta(hours=25))

    def test_complete_yesterday_is_used(self):
        yesterday = date(2026, 11, 13)
        client = _Client([_intervals(yesterday, 96)])
        result = asyncio.run(client._fetch_previous_day_raw_readings("p1", "m1"))
        readings, day, label = result
        self.assertEqual(len(readings), 96)
        self.assertEqual(day, "2026-11-13")
        self.assertEqual(label, "yesterday")
        args = client.fetch_electricity_measurements_range.await_args.args
        self.assertEqual(args[2], "2026-11-12T23:00:00+00:00")
        self.assertEqual(args[3], "2026-11-13T23:00:00+00:00")
        self.assertEqual(args[4], "Europe/Berlin")
        self.assertEqual(args[5], "15min")

    def test_incomplete_yesterday_falls_back_to_complete_earlier_day(self):
        client = _Client(
            [_intervals(date(2026, 11, 13), 40), _intervals(date(2026, 11, 12), 96)]
        )
        _, day, label = asyncio.run(client._fetch_previous_day_raw_readings("p1", "m1"))
        self.assertEqual(day, "2026-11-12")
        self.assertEqual(label, "2_days_ago")

    def test_partial_day_used_when_no_complete_day(self):
        client = _Client([_intervals(date(2026, 11, 13), 40), [], []])
        readings, day, _ = asyncio.run(
            client._fetch_previous_day_raw_readings("p1", "m1")
        )
        self.assertEqual(len(readings), 40)
        self.assertEqual(day, "2026-11-13")

    def test_feed_in_readings_are_ignored(self):
        feed_in = _intervals(date(2026, 11, 13), 96)
        for reading in feed_in:
            reading["reading_direction"] = "FEED_IN"
        client = _Client([feed_in, [], []])
        self.assertIsNone(
            asyncio.run(client._fetch_previous_day_raw_readings("p1", "m1"))
        )

    def test_other_consumption_directions_are_kept(self):
        readings = _intervals(date(2026, 11, 13), 96)
        for index, reading in enumerate(readings):
            reading["reading_direction"] = "IMPORT" if index % 2 else None
        client = _Client([readings])
        result, day, _ = asyncio.run(
            client._fetch_previous_day_raw_readings("p1", "m1")
        )
        self.assertEqual(len(result), 96)
        self.assertEqual(day, "2026-11-13")

    def test_errors_and_missing_malo_return_none(self):
        client = _Client(RuntimeError("backoff"))
        self.assertIsNone(
            asyncio.run(client._fetch_previous_day_raw_readings("p1", "m1"))
        )
        self.assertIsNone(
            asyncio.run(client._fetch_previous_day_raw_readings("p1", None))
        )


class FetchAllDataPreviousDayTests(unittest.TestCase):
    """Raw readings are fetched for MaLo accounts without the smart-meter flag."""

    def _api(self, capabilities):
        api = object.__new__(OctopusGermany)
        api.ensure_token = AsyncMock(return_value=True)
        api.fetch_tariff_capabilities = AsyncMock(return_value=capabilities)
        response = {
            "data": {
                "account": {
                    "allProperties": [
                        {"id": "p0", "electricityMalos": []},
                        {"id": "p1", "electricityMalos": [{"maloNumber": "m1"}]},
                    ]
                }
            }
        }
        api._get_graphql_client = Mock(
            return_value=Mock(execute_async=AsyncMock(return_value=response))
        )
        api.fetch_electricity_smart_meter_readings = AsyncMock(return_value=None)
        api.fetch_electricity_smart_meter_readings_v2 = AsyncMock(return_value=None)
        return api

    def test_raw_readings_used_without_should_receive_flag(self):
        api = self._api(TariffCapabilities())
        readings = _intervals(date(2026, 11, 13), 96)
        api._fetch_previous_day_raw_readings = AsyncMock(
            return_value=(readings, "2026-11-13", "yesterday")
        )
        result = asyncio.run(api.fetch_all_data("account-test"))
        api._fetch_previous_day_raw_readings.assert_awaited_once_with("p1", "m1")
        self.assertEqual(result["electricity_smart_meter_readings"], readings)
        self.assertEqual(result["electricity_smart_meter_readings_date"], "2026-11-13")

    def test_legacy_queries_skipped_without_should_receive_flag(self):
        api = self._api(TariffCapabilities())
        api._fetch_previous_day_raw_readings = AsyncMock(return_value=None)
        result = asyncio.run(api.fetch_all_data("account-test"))
        api.fetch_electricity_smart_meter_readings.assert_not_awaited()
        api.fetch_electricity_smart_meter_readings_v2.assert_not_awaited()
        self.assertNotIn("electricity_smart_meter_readings", result)


if __name__ == "__main__":
    unittest.main()
