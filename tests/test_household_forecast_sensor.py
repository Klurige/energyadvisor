"""Tests for the household load forecast sensor."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.energyadvisor.const import CONF_EXCLUDE_FROM_RECORDING
from custom_components.energyadvisor.coordinators.household_forecast_coordinator import (
    HouseholdForecastCoordinator,
    STATIC_LOAD_FORECAST_KW,
    STATIC_REASON,
    STORE_MODE,
)
from custom_components.energyadvisor.sensor.householdforecastsensor import (
    BaseLoadSensor,
    HouseholdForecastSensor,
    LoadForecastSensor,
)


def _make_sensor(
    *,
    exclude_from_recording: bool = True,
    load_forecast_kw: float = STATIC_LOAD_FORECAST_KW,
    learning_nights: int = 0,
    data_since: str | None = None,
    last_sample_date: str | None = None,
    last_sample_kw: float | None = None,
    reason: str = STATIC_REASON,
) -> HouseholdForecastSensor:
    """Create a household forecast sensor backed by a lightweight stub."""
    coordinator = SimpleNamespace(
        load_forecast_kw=load_forecast_kw,
        base_load_kw=load_forecast_kw,
        household_load_forecast_w=load_forecast_kw * 1000.0,
        household_base_load_w=load_forecast_kw * 1000.0,
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
    *, with_required_entities: bool = True
) -> HouseholdForecastCoordinator:
    """Create a coordinator with a lightweight Home Assistant/config stub."""
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: None)

    entry = MagicMock()
    entry.entry_id = "entry-id"
    entry.options = {}
    if with_required_entities:
        entry.options = {
            "power_meter_consumption": "sensor.household_meter",
            "water_heater_active_entity": "binary_sensor.water_heater_active",
            "central_heating_active_entity": "binary_sensor.central_heating_active",
        }
    return HouseholdForecastCoordinator(hass, entry)


def test_sensor_uses_preferred_entity_id_and_exposes_static_values() -> None:
    """The entity should keep preferred IDs and expose static placeholder values."""
    assert LoadForecastSensor is HouseholdForecastSensor
    assert BaseLoadSensor is HouseholdForecastSensor
    sensor = _make_sensor(exclude_from_recording=False)

    assert sensor.entity_id == "sensor.energy_advisor_base_load"
    assert sensor._attr_suggested_object_id == "load_forecast"
    assert sensor._attr_exclude_from_recording is False
    assert sensor.native_value == 0.0

    attrs = sensor.extra_state_attributes
    assert attrs["household_load_forecast_w"] == 0.0
    assert attrs["household_base_load_w"] == 0.0
    assert attrs["learning_nights"] == 0
    assert attrs["data_since"] is None
    assert attrs["last_sample_date"] is None
    assert attrs["last_sample_kw"] is None
    assert attrs["reason"] == STATIC_REASON


def test_sensor_rounds_values_from_coordinator() -> None:
    """The sensor should still round values from its coordinator surface."""
    sensor = _make_sensor(
        load_forecast_kw=0.8765,
        learning_nights=4,
        data_since="2024-06-01",
        last_sample_date="2024-06-04",
        last_sample_kw=0.9123,
        reason="Static placeholder value.",
    )

    attrs = sensor.extra_state_attributes

    assert sensor.native_value == 0.876
    assert attrs["household_load_forecast_w"] == 876.5
    assert attrs["household_base_load_w"] == 876.5
    assert attrs["learning_nights"] == 4
    assert attrs["data_since"] == "2024-06-01"
    assert attrs["last_sample_date"] == "2024-06-04"
    assert attrs["last_sample_kw"] == 0.912
    assert attrs["reason"] == "Static placeholder value."


def test_coordinator_exposes_static_placeholder_values() -> None:
    """The coordinator should expose a static value while learning is disabled."""
    coordinator = _make_coordinator()

    assert coordinator.load_forecast_kw == STATIC_LOAD_FORECAST_KW
    assert coordinator.base_load_kw == STATIC_LOAD_FORECAST_KW
    assert coordinator.household_load_forecast_w == STATIC_LOAD_FORECAST_KW * 1000.0
    assert coordinator.household_base_load_w == STATIC_LOAD_FORECAST_KW * 1000.0
    assert coordinator.learning_nights == 0
    assert coordinator.data_since is None
    assert coordinator.last_sample_date is None
    assert coordinator.last_sample_kw is None
    assert coordinator.reason == STATIC_REASON


def test_coordinator_callbacks_keep_static_behavior() -> None:
    """The retained callback structure should not alter static output."""
    coordinator = _make_coordinator()

    coordinator._handle_window_start()
    coordinator._handle_quiet_sensor_change(
        SimpleNamespace(data={"new_state": SimpleNamespace(state="on")})
    )
    coordinator._handle_window_finish()

    assert coordinator.load_forecast_kw == STATIC_LOAD_FORECAST_KW
    assert coordinator.learning_nights == 0
    assert coordinator.reason == STATIC_REASON


@pytest.mark.asyncio
async def test_coordinator_setup_normalizes_legacy_storage() -> None:
    """Legacy learning payloads should be replaced by static housekeeping state."""
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

    assert coordinator.load_forecast_kw == STATIC_LOAD_FORECAST_KW
    assert coordinator.reason == STATIC_REASON
    assert len(coordinator._listeners) == 3
    store.async_save.assert_awaited_once_with(
        {"mode": STORE_MODE, "load_forecast_kw": STATIC_LOAD_FORECAST_KW}
    )


@pytest.mark.asyncio
async def test_coordinator_setup_without_required_entities_stays_idle() -> None:
    """Without required entities, the coordinator should skip listener wiring."""
    coordinator = _make_coordinator(with_required_entities=False)
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    coordinator._store = store

    await coordinator.async_setup()

    assert len(coordinator._listeners) == 0
    assert (
        coordinator.reason
        == "Household forecast is waiting for the required meter and quiet-night sensors."
    )


@pytest.mark.asyncio
async def test_coordinator_persists_static_state() -> None:
    """The coordinator should persist static housekeeping payload."""
    coordinator = _make_coordinator()
    store = MagicMock()
    store.async_save = AsyncMock()
    coordinator._store = store

    await coordinator._async_save_state()

    store.async_save.assert_awaited_once_with(
        {"mode": STORE_MODE, "load_forecast_kw": STATIC_LOAD_FORECAST_KW}
    )
