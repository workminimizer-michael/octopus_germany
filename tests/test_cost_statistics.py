"""Tests for quarter-hour price capture and hourly cost statistics."""

# Mock fixtures intentionally mirror the production callback signatures.
# ruff: noqa: ANN001, ANN002, ANN003, ANN201, ARG002, D101, D102, D103, INP001
# ruff: noqa: PT009, PT027, SLF001

import asyncio
import tempfile
import types
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import homeassistant  # noqa: F401
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame
from homeassistant.helpers.recorder import async_initialize_recorder
from homeassistant.setup import async_setup_component

from custom_components.octopus_germany import statistics as s
from custom_components.octopus_germany import statistics_reconciliation as r
from custom_components.octopus_germany.cost_statistics import (
    capture_quarter_prices,
    hourly_costs,
)

AT = datetime(2026, 10, 18, 0, tzinfo=UTC)  # 02:00 local, regular 24-hour day
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
CONSUMPTION = (".1", ".2", ".3", ".4")
PRICES_CT = ("30", "20", "10", "-10")
# 36.5 EUR/month -> 36.5 * 12 / 365 = 1.2 EUR/day -> 0.05 EUR/hour.
STANDING_GROSS_EUR_PER_MONTH = "36.5"
# .1*.3 + .2*.2 + .3*.1 + .4*-.1 = .06 EUR energy + .05 standing share.
EXPECTED_HOUR_COST = Decimal("0.11")


def quarters(at=AT, values=CONSUMPTION):
    return [
        {
            "start_time": (at + i * r.QUARTER).isoformat(),
            "end_time": (at + (i + 1) * r.QUARTER).isoformat(),
            "value": value,
            "unit": "kwh",
        }
        for i, value in enumerate(values)
    ]


def forecast(at=AT, prices_ct=PRICES_CT):
    return [
        {
            "validFrom": (at + i * r.QUARTER).isoformat(),
            "validTo": (at + (i + 1) * r.QUARTER).isoformat(),
            "unitRateInformation": {
                "__typename": "SimpleProductUnitRateInformation",
                "latestGrossUnitRateCentsPerKwh": cents,
            },
        }
        for i, cents in enumerate(prices_ct)
    ]


def dynamic_product(**overrides):
    product = {
        "code": "DYNAMIC",
        "type": "TimeOfUse",
        "isTimeOfUse": True,
        "validFrom": "2026-01-01T00:00:00+01:00",
        "validTo": None,
        "standing_charge_gross_rates": [
            {
                "gross_eur_per_month": STANDING_GROSS_EUR_PER_MONTH,
                "valid_from": "2026-01-01T00:00:00+01:00",
                "valid_to": None,
            }
        ],
        "unitRateForecast": forecast(),
    }
    product.update(overrides)
    return product


def intervals(at=AT, values=CONSUMPTION):
    merged, _, conflicts = r.merge_measurements(
        {}, quarters(at, values), at, at + timedelta(days=1), at, NOW, "malo"
    )
    return merged, conflicts


class CapturePricesTests(unittest.TestCase):
    def test_captures_forecast_quarters_in_eur(self):
        prices, changed = capture_quarter_prices({}, [dynamic_product()], AT)
        self.assertEqual(changed, 4)
        self.assertEqual(prices[AT.isoformat()], "0.3")
        self.assertEqual(prices[(AT + 3 * r.QUARTER).isoformat()], "-0.1")

    def test_recapture_is_idempotent_and_updates_changed_prices(self):
        prices, _ = capture_quarter_prices({}, [dynamic_product()], AT)
        same, changed = capture_quarter_prices(prices, [dynamic_product()], AT)
        self.assertEqual((same, changed), (prices, 0))
        updated = dynamic_product(
            unitRateForecast=forecast(prices_ct=("40", "20", "10", "-10"))
        )
        prices, changed = capture_quarter_prices(prices, [updated], AT)
        self.assertEqual(changed, 1)
        self.assertEqual(prices[AT.isoformat()], "0.4")

    def test_old_prices_are_dropped(self):
        old_key = (AT - timedelta(days=40)).isoformat()
        prices, _ = capture_quarter_prices({old_key: "0.3"}, [dynamic_product()], AT)
        self.assertNotIn(old_key, prices)

    def test_prices_outside_agreement_are_ignored(self):
        product = dynamic_product(validFrom=(AT + timedelta(hours=1)).isoformat())
        prices, changed = capture_quarter_prices({}, [product], AT)
        self.assertEqual((prices, changed), ({}, 0))


class HourlyCostsTests(unittest.TestCase):
    def test_quarter_price_times_consumption_plus_standing_share(self):
        cached, conflicts = intervals()
        prices, _ = capture_quarter_prices({}, [dynamic_product()], AT)
        costs, counts = hourly_costs(
            cached, conflicts, prices, [dynamic_product()], "Europe/Berlin"
        )
        self.assertEqual(costs, {AT: EXPECTED_HOUR_COST})
        self.assertEqual(
            counts, {"missing_price_hours": 0, "missing_standing_charge_hours": 0}
        )

    def test_dynamic_hour_without_captured_price_is_skipped(self):
        cached, conflicts = intervals()
        prices, _ = capture_quarter_prices({}, [dynamic_product()], AT)
        prices.pop((AT + r.QUARTER).isoformat())
        costs, counts = hourly_costs(
            cached, conflicts, prices, [dynamic_product()], "Europe/Berlin"
        )
        self.assertEqual(costs, {})
        self.assertEqual(counts["missing_price_hours"], 1)

    def test_fixed_price_tariff_needs_no_capture(self):
        cached, conflicts = intervals()
        product = dynamic_product(
            isTimeOfUse=False, type="Simple", grossRate="30", unitRateForecast=[]
        )
        costs, _ = hourly_costs(cached, conflicts, {}, [product], "Europe/Berlin")
        # 1.0 kWh * 0.30 EUR + 0.05 EUR standing share.
        self.assertEqual(costs, {AT: Decimal("0.35")})

    def test_dst_day_spreads_standing_charge_over_25_hours(self):
        at = datetime(2026, 10, 25, 0, tzinfo=UTC)
        cached, conflicts = intervals(at)
        product = dynamic_product(unitRateForecast=forecast(at))
        prices, _ = capture_quarter_prices({}, [product], at)
        costs, _ = hourly_costs(cached, conflicts, prices, [product], "Europe/Berlin")
        self.assertEqual(costs[at], Decimal("0.06") + Decimal("1.2") / 25)

    def test_missing_standing_charge_is_counted(self):
        cached, conflicts = intervals()
        product = dynamic_product(standing_charge_gross_rates=[])
        prices, _ = capture_quarter_prices({}, [product], AT)
        costs, counts = hourly_costs(
            cached, conflicts, prices, [product], "Europe/Berlin"
        )
        self.assertEqual(costs, {AT: Decimal("0.06")})
        self.assertEqual(counts["missing_standing_charge_hours"], 1)


class RebuildCostTests(unittest.TestCase):
    def test_negative_amounts_are_accepted_by_cost_parser(self):
        changes, _ = r.rebuild_statistics(
            [],
            {AT: Decimal("-0.02"), AT + timedelta(hours=1): Decimal("0.05")},
            AT,
            NOW,
            value_parser=r.decimal_amount,
        )
        self.assertEqual([row["sum"] for row in changes], [-0.02, 0.03])
        # Existing negative cost rows are read back with the cost parser,
        # while the energy default still rejects them.
        existing = [{"start": AT, "state": -0.02, "sum": -0.02}]
        later = {AT + timedelta(hours=1): Decimal("0.05")}
        changes, _ = r.rebuild_statistics(
            existing, later, AT, NOW, value_parser=r.decimal_amount
        )
        self.assertEqual(changes[-1]["sum"], 0.03)
        with self.assertRaises(ValueError):
            r.rebuild_statistics(existing, later, AT, NOW)


class CostImporterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.persisted = None
        self.rows: dict[str, list[dict]] = {}
        self.metadata: dict[str, dict] = {}
        self.api = types.SimpleNamespace(
            fetch_electricity_measurements_range=AsyncMock(return_value=quarters())
        )
        self.coordinator = types.SimpleNamespace(
            data={
                "A-TEST": {
                    "property_ids": ["p"],
                    "malo_number": "malo",
                    "meter": {"number": "test"},
                    "products": [dynamic_product()],
                }
            },
            async_add_listener=MagicMock(return_value=MagicMock()),
        )
        self.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone="Europe/Berlin"),
            loop=asyncio.get_running_loop(),
        )
        self.store = types.SimpleNamespace(
            async_load=AsyncMock(return_value=None),
            async_save=AsyncMock(side_effect=self.save),
        )
        self.recorder = types.SimpleNamespace(
            async_add_executor_job=AsyncMock(side_effect=self.read),
            queue_task=MagicMock(side_effect=lambda task: task.future.set_result(None)),
        )
        self.patches = [
            patch.object(s, "Store", return_value=self.store),
            patch.object(s, "get_instance", return_value=self.recorder),
            patch.object(s, "async_add_external_statistics", side_effect=self.write),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.importer = s.StatisticsReconciler(
            self.hass, self.api, self.coordinator, ["A-TEST"], "entry"
        )

    async def save(self, data):
        self.persisted = deepcopy(data)

    async def read(self, fn, hass, start, end, ids, *args):
        statistic_id = next(iter(ids))
        return {
            statistic_id: [
                deepcopy(x)
                for x in self.rows.get(statistic_id, [])
                if x["start"] >= start
            ]
        }

    def write(self, hass, metadata, changes):
        statistic_id = metadata["statistic_id"]
        self.metadata[statistic_id] = metadata
        rows = {x["start"]: x for x in self.rows.get(statistic_id, [])}
        rows.update({x["start"]: deepcopy(x) for x in changes})
        self.rows[statistic_id] = list(rows.values())

    async def test_cost_statistic_written_in_eur_next_to_consumption(self):
        await self.importer.async_run(NOW)
        cost_id = "octopus_germany:electricity_a_test_cost"
        consumption_id = "octopus_germany:electricity_a_test_consumption"
        self.assertEqual(self.rows[consumption_id][0]["state"], 1.0)
        self.assertEqual(len(self.rows[cost_id]), 1)
        self.assertAlmostEqual(self.rows[cost_id][0]["state"], 0.11)
        self.assertAlmostEqual(self.rows[cost_id][0]["sum"], 0.11)
        self.assertEqual(self.metadata[cost_id]["unit_of_measurement"], "EUR")
        self.assertIsNone(self.metadata[cost_id]["unit_class"])
        self.assertEqual(self.metadata[consumption_id]["unit_of_measurement"], "kWh")
        self.assertEqual(
            len(self.persisted["accounts"]["A-TEST"]["prices"]), len(PRICES_CT)
        )

    async def test_prices_survive_when_forecast_is_gone(self):
        await self.importer.async_run(NOW)
        self.coordinator.data["A-TEST"]["products"] = [
            dynamic_product(unitRateForecast=[])
        ]
        self.importer.next_scheduled_run = None
        await self.importer.async_run(NOW)
        cost_id = "octopus_germany:electricity_a_test_cost"
        self.assertAlmostEqual(self.rows[cost_id][0]["sum"], 0.11)
        self.assertEqual(
            len(self.importer.data["accounts"]["A-TEST"]["prices"]), len(PRICES_CT)
        )

    async def test_prices_are_captured_even_when_fetch_is_empty(self):
        self.api.fetch_electricity_measurements_range.return_value = []
        await self.importer.async_run(NOW)
        self.assertEqual(
            len(self.persisted["accounts"]["A-TEST"]["prices"]), len(PRICES_CT)
        )

    async def test_cost_failure_does_not_block_consumption_checkpoint(self):
        with (
            patch.object(s, "hourly_costs", side_effect=RuntimeError("boom")),
            self.assertLogs(s._LOGGER, level="ERROR"),
        ):
            await self.importer.async_run(NOW)
        self.assertEqual(self.importer.status["phase"], "complete")
        self.assertIn("intervals", self.persisted["accounts"]["A-TEST"])


class RealRecorderCostTests(unittest.IsolatedAsyncioTestCase):
    async def test_cost_statistic_round_trips_through_real_recorder(self):
        with tempfile.TemporaryDirectory(prefix="octopus-cost-test-") as directory:
            hass = HomeAssistant(directory)
            hass.config.time_zone = "Europe/Berlin"
            loader.async_setup(hass)
            frame.async_setup(hass)
            async_initialize_recorder(hass)
            hass.config_entries = ConfigEntries(hass, {})
            await hass.config_entries.async_initialize()
            try:
                self.assertTrue(
                    await async_setup_component(
                        hass,
                        "recorder",
                        {
                            "recorder": {
                                "db_url": f"sqlite:///{directory}/recorder.db",
                                "auto_purge": False,
                                "auto_repack": False,
                            }
                        },
                    )
                )
                await hass.async_start()
                api = types.SimpleNamespace(
                    fetch_electricity_measurements_range=AsyncMock(
                        return_value=quarters()
                    )
                )
                coordinator = types.SimpleNamespace(
                    data={
                        "A-TEST": {
                            "property_ids": ["p"],
                            "malo_number": "malo",
                            "products": [dynamic_product()],
                        }
                    }
                )
                importer = s.StatisticsReconciler(
                    hass, api, coordinator, ["A-TEST"], "cost-entry"
                )
                await importer.async_run(NOW)
                await importer.async_run(NOW)
                recorder = s.get_instance(hass)
                statistic_id = "octopus_germany:electricity_a_test_cost"
                result = await recorder.async_add_executor_job(
                    s.statistics_during_period,
                    hass,
                    AT,
                    None,
                    {statistic_id},
                    "hour",
                    None,
                    {"state", "sum"},
                )
                rows = result[statistic_id]
                self.assertEqual(len(rows), 1)
                self.assertAlmostEqual(rows[0]["state"], 0.11)
                self.assertAlmostEqual(rows[0]["sum"], 0.11)
            finally:
                await hass.async_stop()


if __name__ == "__main__":
    unittest.main()
