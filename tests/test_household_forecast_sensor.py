"""Tests for the household base-load forecast sensor."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.energyadvisor.const import CONF_EXCLUDE_FROM_RECORDING
from custom_components.energyadvisor.coordinators.household_forecast_coordinator import (
    HouseholdForecastCoordinator,
)
from custom_components.energyadvisor.sensor.householdforecastsensor import (
    BaseLoadSensor,
    HouseholdForecastSensor,
)


def _make_sensor(
    *,
    exclude_from_recording: bool = True,
    base_load_kw: float | None = None,
    learning_nights: int = 0,
    data_since: str | None = None,
    last_sample_date: str | None = None,
    last_sample_kw: float | None = None,
    reason: str = "Waiting for the first quiet-night sample.",
) -> HouseholdForecastSensor:
    """Create a household forecast sensor backed by a lightweight stub."""
    coordinator = SimpleNamespace(
        base_load_kw=base_load_kw,
        household_base_load_w=None if base_load_kw is None else base_load_kw * 1000.0,
        learning_nights=learning_nights,
        data_since=data_since,
        last_sample_date=last_sample_date,
        last_sample_kw=last_sample_kw,
        reason=reason,
        register_update_callback=MagicMock(),
        unregister_update_callback=MagicMock(),
    )
    entry = MagicMock()
    entry.entry_id = "entry-id"
    entry.unique_id = "sensor.nord_pool_se4_current_price"
    entry.options = {CONF_EXCLUDE_FROM_RECORDING: exclude_from_recording}
    return HouseholdForecastSensor(MagicMock(), entry, {}, coordinator)


def _make_coordinator(
    *,
    meter_state: str = "100.0",
    meter_unit: str = "kWh",
    water_state: str = "off",
    heat_state: str = "off",
) -> HouseholdForecastCoordinator:
    """Create a coordinator with a simple state lookup stub."""
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.time_zone = "Europe/Stockholm"
    hass.async_create_task = MagicMock(side_effect=lambda coro: None)

    meter = SimpleNamespace(state=meter_state, attributes={"unit_of_measurement": meter_unit})
    water = SimpleNamespace(state=water_state, attributes={})
    heat = SimpleNamespace(state=heat_state, attributes={})
    hass.states = MagicMock()
    hass.states.get.side_effect = lambda entity_id: {
        "sensor.household_meter": meter,
        "binary_sensor.water_heater_active": water,
        "binary_sensor.central_heating_active": heat,
    }.get(entity_id)

    entry = MagicMock()
    entry.options = {
        "power_meter_consumption": "sensor.household_meter",
        "water_heater_active_entity": "binary_sensor.water_heater_active",
        "central_heating_active_entity": "binary_sensor.central_heating_active",
    }
    return HouseholdForecastCoordinator(hass, entry)


def test_sensor_uses_preferred_entity_id_and_exposes_learning_reason() -> None:
    """The entity should use the preferred ID and expose the waiting state."""
    assert BaseLoadSensor is HouseholdForecastSensor
    sensor = _make_sensor(exclude_from_recording=False)

    assert sensor.entity_id == "sensor.energy_advisor_base_load"
    assert sensor._attr_suggested_object_id == "base_load"
    assert sensor._attr_exclude_from_recording is False
    assert sensor.native_value is None

    attrs = sensor.extra_state_attributes
    assert attrs["learning_nights"] == 0
    assert attrs["household_base_load_w"] is None
    assert attrs["reason"] == "Waiting for the first quiet-night sample."


def test_sensor_reports_rolling_average_and_summary_attributes() -> None:
    """The sensor should surface the current rolling average and latest sample."""
    sensor = _make_sensor(
        base_load_kw=0.8765,
        learning_nights=4,
        data_since="2024-06-01",
        last_sample_date="2024-06-04",
        last_sample_kw=0.9123,
        reason="Using the average of 4 quiet nights.",
    )

    attrs = sensor.extra_state_attributes

    assert sensor.native_value == 0.876
    assert attrs["household_base_load_w"] == 876.5
    assert attrs["learning_nights"] == 4
    assert attrs["data_since"] == "2024-06-01"
    assert attrs["last_sample_date"] == "2024-06-04"
    assert attrs["last_sample_kw"] == 0.912
    assert attrs["reason"] == "Using the average of 4 quiet nights."


def test_coordinator_learns_a_quiet_night_sample() -> None:
    """A quiet 01:00-04:00 window should yield a new average sample."""
    coordinator = _make_coordinator(meter_state="103.0")
    now = datetime(2026, 8, 15, 4, 0, tzinfo=ZoneInfo("Europe/Stockholm"))

    coordinator._window_date = now.date()
    coordinator._window_start_kwh = 100.0
    coordinator._window_invalid = False
    coordinator._window_skip_reason = None

    with patch(
        "custom_components.energyadvisor.coordinators.household_forecast_coordinator.dt_util.now",
        return_value=now,
    ):
        coordinator._handle_window_finish()

    assert coordinator.base_load_kw == 1.0
    assert coordinator.learning_nights == 1
    assert coordinator.data_since == "2026-08-15"
    assert coordinator.last_sample_date == "2026-08-15"
    assert coordinator.last_sample_kw == 1.0
    assert coordinator.reason == "Using the average of 1 quiet night."


def test_coordinator_skips_when_a_quiet_sensor_turns_on() -> None:
    """A quiet-night window must be rejected if a monitored load turns on."""
    coordinator = _make_coordinator()
    now = datetime(2026, 8, 15, 2, 0, tzinfo=ZoneInfo("Europe/Stockholm"))

    coordinator._window_date = now.date()
    coordinator._window_start_kwh = 100.0
    coordinator._window_invalid = False
    coordinator._window_skip_reason = None

    with patch(
        "custom_components.energyadvisor.coordinators.household_forecast_coordinator.dt_util.now",
        return_value=now,
    ):
        coordinator._handle_quiet_sensor_change(
            SimpleNamespace(data={"new_state": SimpleNamespace(state="on")})
        )

    assert coordinator._window_invalid is True
    assert "skipped" in coordinator.reason.lower()
    assert coordinator.learning_nights == 0


@pytest.mark.asyncio
async def test_coordinator_restores_persisted_samples_and_window_state() -> None:
    """Persisted quiet-night samples should survive a restart."""
    now = datetime(2026, 8, 15, 8, 0, tzinfo=ZoneInfo("Europe/Stockholm"))
    payload = {
        "samples": [
            {"date": "2026-08-14", "base_load_kw": 0.8},
            {"date": "2026-08-15", "base_load_kw": 1.2},
        ],
        "window": {
            "date": "2026-08-15",
            "start_kwh": 100.0,
            "invalid": False,
            "skip_reason": None,
        },
    }

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store"
        ) as mock_store_class,
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ),
    ):
        store = MagicMock()
        store.async_load = AsyncMock(return_value=payload)
        store.async_save = AsyncMock()
        mock_store_class.return_value = store

        coordinator = _make_coordinator()
        await coordinator.async_setup()

    assert coordinator.learning_nights == 2
    assert coordinator.base_load_kw == pytest.approx(1.0)
    assert coordinator.data_since == "2026-08-14"
    assert coordinator.last_sample_date == "2026-08-15"
    assert coordinator.last_sample_kw == pytest.approx(1.2)
    assert coordinator.reason == "Using the average of 2 quiet nights."
    assert coordinator._window_date == date(2026, 8, 15)
    assert coordinator._window_start_kwh == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_coordinator_persists_samples_and_window_state() -> None:
    """The coordinator should save learned samples and the active window."""
    coordinator = _make_coordinator()
    store = MagicMock()
    store.async_save = AsyncMock()
    coordinator._store = store
    coordinator._samples = {
        date(2026, 8, 14): 0.8,
        date(2026, 8, 15): 1.2,
    }
    coordinator._window_date = date(2026, 8, 15)
    coordinator._window_start_kwh = 100.0
    coordinator._window_invalid = True
    coordinator._window_skip_reason = "The sample was skipped."

    await coordinator._async_save_state()

    store.async_save.assert_awaited_once()
    assert store.async_save.call_args.args[0] == {
        "samples": [
            {"date": "2026-08-14", "base_load_kw": 0.8},
            {"date": "2026-08-15", "base_load_kw": 1.2},
        ],
        "window": {
            "date": "2026-08-15",
            "start_kwh": 100.0,
            "invalid": True,
            "skip_reason": "The sample was skipped.",
        },
    }
