import asyncio
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.energyadvisor.const import DOMAIN
from custom_components.energyadvisor.sensor.compactlevels import (
    CompactLevelsSensor,
    calculate_levels,
)


@pytest.fixture
def hass():
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.time_zone = "UTC"
    hass.loop = asyncio.new_event_loop()
    hass.data = {"custom_components": {}, DOMAIN: {}}
    hass.loop_thread_id = threading.get_ident()
    hass.async_create_task = lambda coro: asyncio.create_task(coro)
    hass.async_create_background_task = lambda coro, _name: asyncio.create_task(coro)
    return hass


@pytest.fixture
def entry():
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.unique_id = "sensor.nord_pool_se4_current_price"
    entry.options = {}
    return entry


@pytest.fixture
def device_info():
    return MagicMock()


@pytest.fixture
def source_sensor():
    sensor = MagicMock()
    sensor.has_rates = False
    sensor.async_add_update_listener.return_value = lambda: None
    sensor.build_levels_payload.return_value = {
        "level_length": 12,
        "levels": "ABCDEFGHIJKLMNOPQRST" * 12,
    }
    return sensor


@pytest.fixture
def sensor(hass, entry, device_info, source_sensor):
    compact = CompactLevelsSensor(hass, entry, device_info, source_sensor)
    compact.hass = hass
    compact.entity_id = "sensor.levels"
    compact.async_on_remove = MagicMock()
    return compact


def test_constructor_uses_preferred_entity_id(hass, entry, device_info, source_sensor):
    """The compact sensor should use the staged preferred entity ID."""
    compact = CompactLevelsSensor(hass, entry, device_info, source_sensor)
    assert compact.entity_id == "sensor.energy_advisor_compact_levels"


@pytest.mark.asyncio
async def test_async_added_to_hass_calls_start_on_available(sensor, source_sensor):
    source_sensor.has_rates = True
    with patch.object(
        sensor, "_start_levels_sensor", new_callable=AsyncMock
    ) as mock_start:
        await sensor.async_added_to_hass()
    mock_start.assert_awaited_once()
    source_sensor.async_add_update_listener.assert_called_once()
    sensor.async_on_remove.assert_called_once()


@pytest.mark.asyncio
async def test_async_added_to_hass_does_not_call_start_without_rates(
    sensor, source_sensor
):
    source_sensor.has_rates = False
    with patch.object(
        sensor, "_start_levels_sensor", new_callable=AsyncMock
    ) as mock_start:
        await sensor.async_added_to_hass()
    mock_start.assert_not_awaited()
    source_sensor.async_add_update_listener.assert_called_once()
    sensor.async_on_remove.assert_called_once()


@pytest.mark.asyncio
async def test_handle_source_update_triggers_start(sensor):
    sensor._waiting_for_first_value = True
    with patch.object(
        sensor, "_start_levels_sensor", new_callable=AsyncMock
    ) as mock_start:
        sensor._handle_source_update()
        await asyncio.sleep(0)
    mock_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_source_update_refreshes_when_running(sensor):
    sensor._waiting_for_first_value = False
    with patch.object(
        sensor, "_refresh_from_source", new_callable=AsyncMock
    ) as mock_refresh:
        sensor._handle_source_update()
        await asyncio.sleep(0)
    mock_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_levels_sensor_idempotent(sensor):
    sensor._waiting_for_first_value = False
    await sensor._start_levels_sensor()
    assert sensor._waiting_for_first_value is False


@patch("custom_components.energyadvisor.sensor.compactlevels.datetime")
def test_fetch_service_value_normal(mock_dt, sensor, source_sensor):
    mock_now = datetime(2025, 1, 1, 1, 12, 0)
    mock_dt.now.return_value = mock_now

    source_sensor.build_levels_payload.return_value = {
        "level_length": 12,
        "levels": "ABCDEFGHIJKLMNOPQRST" * 12,
    }
    minutes_since_midnight, value, _ = sensor._fetch_compact_values()
    assert minutes_since_midnight == 72
    assert isinstance(value["compact"], str)
    parts = value["compact"].split(":")
    assert len(parts) == 4
    assert int(parts[0]) == minutes_since_midnight
    assert int(parts[1]) == 12
    assert parts[2] == "BCDEF"
    assert parts[3] == "GHIJKLMNOPQRSTABCDEFGHIJKLMNOPQRSTABCDEFGHIJKLMNOPQRSTABCDEF"


def test_fetch_service_value_no_data(sensor, source_sensor):
    source_sensor.build_levels_payload.return_value = {"level_length": 0, "levels": ""}
    minutes_since_midnight, value, _ = sensor._fetch_compact_values()
    assert isinstance(value["compact"], str)
    parts = value["compact"].split(":")
    assert len(parts) == 4
    assert int(parts[0]) == minutes_since_midnight
    assert int(parts[1]) == 0
    assert parts[2] == ""
    assert parts[3] == ""


def test_fetch_service_value_all_unknown(sensor, source_sensor):
    source_sensor.build_levels_payload.return_value = {"level_length": 0, "levels": ""}
    minutes_since_midnight, value, next_update = sensor._fetch_compact_values()
    assert next_update == 5
    parts = value["compact"].split(":")
    assert len(parts) == 4
    assert int(parts[0]) == minutes_since_midnight
    assert int(parts[1]) == 0
    assert parts[2] == ""
    assert parts[3] == ""


@pytest.mark.asyncio
async def test_async_will_remove_from_hass(sensor):
    sensor._task = MagicMock()
    await sensor.async_will_remove_from_hass()
    sensor._task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_periodic_update(sensor):
    with patch.object(
        sensor, "_refresh_from_source", new_callable=AsyncMock
    ) as mock_refresh:
        mock_refresh.return_value = 1
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

            async def stop_after_one(*args, **kwargs):
                raise asyncio.CancelledError()

            mock_sleep.side_effect = stop_after_one
            with pytest.raises(asyncio.CancelledError):
                await sensor._periodic_update()
            mock_refresh.assert_awaited()
            mock_sleep.assert_called_once_with(1)


@patch("custom_components.energyadvisor.sensor.compactlevels.dt_util.get_time_zone")
@patch("custom_components.energyadvisor.sensor.compactlevels.datetime")
def test_fetch_service_value_now_and_next(mock_dt, mock_tz, sensor, source_sensor):
    mock_tz.return_value = "UTC"
    mock_now = datetime(2023, 1, 1, 10, 15, 30)
    mock_dt.now.return_value = mock_now

    source_sensor.build_levels_payload.return_value = {
        "level_length": 12,
        "levels": "ABCDEFGHIJKLMNOPQRST" * 12,
    }

    minutes_since_midnight, value, next_update = sensor._fetch_compact_values()
    assert minutes_since_midnight == 615
    assert next_update == 510
    assert value == {
        "compact": "615:12:GHIJK:LMNOPQRSTABCDEFGHIJKLMNOPQRSTABCDEFGHIJKLMNOPQRSTABCDEFGHIJK"
    }

    mock_now = mock_now + timedelta(seconds=next_update)
    mock_dt.now.return_value = mock_now
    minutes_since_midnight, value, next_update = sensor._fetch_compact_values()
    assert minutes_since_midnight == 624
    assert value == {
        "compact": "624:12:HIJKL:MNOPQRSTABCDEFGHIJKLMNOPQRSTABCDEFGHIJKLMNOPQRSTABCDEFGHIJKL"
    }
    assert next_update == 720

    mock_now = mock_now + timedelta(seconds=next_update)
    mock_dt.now.return_value = mock_now
    minutes_since_midnight, value, next_update = sensor._fetch_compact_values()
    assert minutes_since_midnight == 636
    assert value == {
        "compact": "636:12:IJKLM:NOPQRSTABCDEFGHIJKLMNOPQRSTABCDEFGHIJKLMNOPQRSTABCDEFGHIJKLM"
    }
    assert next_update == 720


def test_calculate_levels_resolves_loaded_sensor(hass, source_sensor):
    source_sensor.entity_id = "sensor.energy_advisor_price"
    source_sensor.build_levels_payload.return_value = {
        "level_length": 60,
        "levels": "LM",
        "low_threshold": 1.0,
        "high_threshold": 2.0,
    }
    hass.data[DOMAIN]["entry"] = SimpleNamespace(levels_sensor=source_sensor)

    result = calculate_levels(hass, requested_length=60)

    assert result["levels"] == "LM"
    source_sensor.build_levels_payload.assert_called_once_with(
        requested_length=60,
        fill_unknown=False,
    )
