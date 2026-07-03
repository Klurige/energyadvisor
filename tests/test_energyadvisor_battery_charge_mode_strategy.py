"""Tests for the battery charge mode strategy helpers."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from unittest.mock import patch

from custom_components.energyadvisor.sensor.battery_charge_mode_strategy import (
    _apply_summer_sell_strategy,
    _classify_output_modes,
    _parse_compact_rates,
)

UTC = timezone.utc


def _make_rates(
    start: datetime, costs: list[float], credits: list[float] | None = None
) -> list[dict]:
    if credits is None:
        credits = costs
    return [
        {
            "from": (start + timedelta(hours=index)).isoformat(timespec="minutes"),
            "cost": cost,
            "credit": credits[index],
        }
        for index, cost in enumerate(costs)
    ]


def _make_solar_entries(start: datetime, powers: list[float]) -> list[dict]:
    return [
        {
            "end": (start + timedelta(minutes=15 * (index + 1))).isoformat(timespec="minutes"),
            "pow": power,
        }
        for index, power in enumerate(powers)
    ]


def _make_charge_entries(start: datetime, costs: list[float]) -> list[dict]:
    return [
        {
            "start": start + timedelta(hours=index),
            "end": start + timedelta(hours=index + 1),
            "mode": "standby",
            "cost": cost,
            "credit": cost,
        }
        for index, cost in enumerate(costs)
    ]


def test_parse_compact_rates_skips_malformed_rows_and_uses_local_timezone():
    local_tz = ZoneInfo("Europe/Stockholm")
    rates = [
        {"from": "2024-06-01T00:00", "cost": "1.0", "credit": "1.5"},
        {"from": "not-a-date", "cost": 2.0, "credit": 2.0},
        {"cost": 3.0, "credit": 3.0},
        {"from": "2024-06-01T01:00", "cost": 4.0, "credit": 5.0},
    ]

    with patch(
        "custom_components.energyadvisor.sensor.battery_charge_mode_strategy.dt_util.get_default_time_zone",
        return_value=local_tz,
    ):
        parsed = _parse_compact_rates(rates)

    assert len(parsed) == 2
    assert parsed[0]["start"] == datetime(2024, 6, 1, 0, 0, tzinfo=local_tz)
    assert parsed[0]["end"] == datetime(2024, 6, 1, 1, 0, tzinfo=local_tz)
    assert parsed[0]["cost"] == 1.0
    assert parsed[0]["credit"] == 1.5
    assert parsed[1]["start"] == datetime(2024, 6, 1, 1, 0, tzinfo=local_tz)
    assert parsed[1]["end"] == datetime(2024, 6, 1, 2, 0, tzinfo=local_tz)
    assert parsed[1]["cost"] == 4.0
    assert parsed[1]["credit"] == 5.0


def test_apply_summer_sell_strategy_chooses_contiguous_sell_window():
    charge_entries = _make_charge_entries(datetime(2024, 6, 1, 0, 0, tzinfo=UTC), [1.0] * 24)
    charge_entries[17]["credit"] = 5.0
    charge_entries[18]["credit"] = 10.0
    charge_entries[19]["credit"] = 8.0
    charge_entries[20]["credit"] = 4.0
    charge_entries[21]["credit"] = 3.0

    solar_entries = _make_solar_entries(
        datetime(2024, 6, 1, 12, 0, tzinfo=UTC), [1.0] * 24
    )

    _apply_summer_sell_strategy(
        charge_entries,
        sellable_kwh=9.0,
        discharge_power_kw=4.0,
        solar_entries=solar_entries,
        margin=0.7,
    )

    assert sum(entry["mode"] == "sell" for entry in charge_entries) == 3
    assert {entry["start"].hour for entry in charge_entries if entry["mode"] == "sell"} == {
        17,
        18,
        19,
    }
    assert all(entry["mode"] == "maxuse" for entry in charge_entries[12:17])
    assert charge_entries[20]["mode"] == "discharge"


def test_classify_output_modes_respects_extension_and_sell_margin():
    base = datetime(2024, 6, 1, 0, 0, tzinfo=UTC)
    charge_entries = [
        {
            "start": base,
            "end": base + timedelta(hours=1),
            "mode": "discharge",
            "cost": 6.0,
            "credit": 6.0,
            "mode_source": "extension_head",
        },
        {
            "start": base + timedelta(hours=1),
            "end": base + timedelta(hours=2),
            "mode": "charge",
            "cost": 1.0,
            "credit": 1.0,
        },
        {
            "start": base + timedelta(hours=2),
            "end": base + timedelta(hours=3),
            "mode": "discharge",
            "cost": 1.5,
            "credit": 1.5,
            "mode_source": "peak",
        },
        {
            "start": base + timedelta(hours=3),
            "end": base + timedelta(hours=4),
            "mode": "charge",
            "cost": 2.0,
            "credit": 2.0,
        },
        {
            "start": base + timedelta(hours=4),
            "end": base + timedelta(hours=5),
            "mode": "discharge",
            "cost": 3.0,
            "credit": 8.0,
            "mode_source": "peak",
        },
        {
            "start": base + timedelta(hours=5),
            "end": base + timedelta(hours=6),
            "mode": "charge",
            "cost": 1.0,
            "credit": 1.0,
        },
    ]

    _classify_output_modes(charge_entries, margin=0.7)

    assert charge_entries[0]["mode"] == "maxuse"
    assert charge_entries[2]["mode"] == "discharge"
    assert charge_entries[4]["mode"] == "sell"
