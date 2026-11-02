"""Compact battery modes follow published plans, not just price updates."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.entity import EntityCategory

from custom_components.energyadvisor.sensor.batterychargemodesensor import (
    MODE_TO_COMPACT,
    BatteryChargeModeSensor,
    Schedule,
)
from custom_components.energyadvisor.sensor.compactbatterymodes import (
    CompactBatteryModesSensor,
)

NOW = datetime(2026, 11, 2, 10, 15, 30, tzinfo=ZoneInfo("Europe/Stockholm"))


@pytest.fixture
def battery(hass):
    entry = MagicMock(entry_id="entry", unique_id="unique", options={})
    source = BatteryChargeModeSensor(hass, entry, MagicMock(), MagicMock())
    source.async_write_ha_state = MagicMock()
    return source


def _schedule(start, modes, length=15):
    return Schedule(
        [
            {
                "from": (start + timedelta(minutes=i * length)).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
                "to": (start + timedelta(minutes=(i + 1) * length)).strftime(
                    "%Y-%m-%dT%H:%M"
                ),
                "mode": mode,
                "target_soc": 50,
                "cost": 1.0,
                "credit": 0.0,
            }
            for i, mode in enumerate(modes)
        ],
        modes[0] if modes else "standby",
        (),
    )


@pytest.fixture
async def compact(hass, battery):
    await hass.config.async_set_time_zone("Europe/Stockholm")
    entry = MagicMock(entry_id="entry", unique_id="unique", options={})
    sensor = CompactBatteryModesSensor(hass, entry, MagicMock(), battery)
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()
    with patch(
        "custom_components.energyadvisor.sensor.compactbatterymodes.dt_util.now",
        return_value=NOW,
    ):
        await sensor.async_added_to_hass()
        yield sensor
        await sensor.async_remove()


def test_modes_payload_maps_all_modes_and_preserves_gaps(battery):
    midnight = NOW.replace(hour=0, minute=0, second=0)
    battery._apply_schedule(_schedule(midnight, list(MODE_TO_COMPACT)))
    length, modes = battery.build_modes_payload(NOW)
    assert (length, modes) == (15, "SCPDEM")

    battery._modes.pop(1)
    assert battery.build_modes_payload(NOW) == (15, "SUPDEM")
    assert battery.build_modes_payload(NOW + timedelta(days=1)) == (15, "")


def test_empty_modes_payload(battery):
    assert battery.build_modes_payload(NOW) == (0, "")


def test_modes_payload_spans_midnight(battery):
    start = NOW.replace(hour=23, minute=45, second=0)
    battery._apply_schedule(_schedule(start, ["charge", "sell", "maxuse"]))
    assert battery.build_modes_payload(NOW) == (15, "U" * 95 + "CEM")
    assert battery.build_modes_payload(NOW + timedelta(days=1)) == (15, "EM")


@pytest.mark.parametrize("length", [15, 30, 60])
def test_modes_payload_keeps_native_slot_length(battery, length):
    start = NOW.replace(hour=0, minute=0, second=0)
    battery._apply_schedule(_schedule(start, ["charge", "discharge"], length))
    assert battery.build_modes_payload(NOW) == (length, "CD")


async def test_diagnostic_defaults_and_empty_state(compact):
    assert compact.entity_id == "sensor.energy_advisor_compact_battery_modes"
    assert compact.entity_category == EntityCategory.DIAGNOSTIC
    assert compact.entity_registry_enabled_default is False
    assert compact.should_poll is False
    assert compact.native_value == 615
    assert compact.extra_state_attributes == {"compact": "615:0::"}


async def test_plan_update_refreshes_without_price_update(compact, battery):
    start = NOW.replace(hour=9, minute=15, second=0)
    battery._apply_schedule(
        _schedule(
            start,
            ["standby"] * 4 + ["charge", "pvcharge", "discharge", "sell", "maxuse"],
        )
    )
    await asyncio.sleep(0)
    assert compact.extra_state_attributes == {"compact": "615:15:SSSS:CPDEM" + "U" * 43}


async def test_burst_is_coalesced_to_latest_plan(compact, battery):
    start = NOW.replace(minute=15, second=0)
    with patch.object(
        battery, "build_modes_payload", wraps=battery.build_modes_payload
    ) as build:
        compact.async_write_ha_state.reset_mock()
        for i in range(1000):
            battery._apply_schedule(
                _schedule(start, ["charge" if i % 2 == 0 else "sell"])
            )
        await asyncio.sleep(0)
    build.assert_called_once()
    compact.async_write_ha_state.assert_called_once()
    assert compact.extra_state_attributes["compact"] == "615:15:UUUU:E" + "U" * 47


async def test_identical_compact_payload_does_not_write(compact, battery):
    start = NOW.replace(minute=15, second=0)
    schedule = _schedule(start, ["charge"])
    battery._apply_schedule(schedule)
    await asyncio.sleep(0)
    compact.async_write_ha_state.reset_mock()
    schedule.modes[0]["target_soc"] = 80
    battery._apply_schedule(schedule)
    await asyncio.sleep(0)
    compact.async_write_ha_state.assert_not_called()


async def test_slot_timer_is_replaced_on_replan_and_advances(compact, battery, hass):
    start = NOW.replace(minute=15, second=0)
    battery._apply_schedule(_schedule(start, ["charge", "discharge"]))
    await asyncio.sleep(0)
    timer = compact._timer
    assert timer.when() - hass.loop.time() == pytest.approx(870, abs=1)
    battery._apply_schedule(_schedule(start, ["sell", "maxuse"]))
    await asyncio.sleep(0)
    assert timer.cancelled()
    with patch(
        "custom_components.energyadvisor.sensor.compactbatterymodes.dt_util.now",
        return_value=NOW.replace(minute=30, second=0),
    ):
        compact._refresh()
    assert compact.extra_state_attributes["compact"] == "630:15:UUUE:M" + "U" * 47
    assert compact._timer.when() - hass.loop.time() == pytest.approx(900, abs=1)


async def test_removal_cancels_pending_refresh_and_timer(compact, battery):
    battery._apply_schedule(_schedule(NOW.replace(second=0), ["charge"]))
    pending = compact._pending_refresh
    timer = compact._timer
    await compact.async_remove()
    assert pending.cancelled()
    assert timer.cancelled()
    assert not battery._update_listeners
    compact.async_write_ha_state.reset_mock()
    await asyncio.sleep(0)
    compact.async_write_ha_state.assert_not_called()


def test_schedule_listener_can_be_removed(battery):
    listener = MagicMock()
    remove = battery.async_add_update_listener(listener)
    battery._apply_schedule(_schedule(NOW, ["standby"]))
    listener.assert_called_once()
    remove()
    remove()
    battery._apply_schedule(_schedule(NOW, ["charge"]))
    listener.assert_called_once()
