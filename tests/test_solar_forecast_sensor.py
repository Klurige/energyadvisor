"""Tests for the solar forecast sensor entity."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from custom_components.electricitypricelevels.const import (
    ATTR_DATA_SINCE,
    ATTR_ENERGY_TODAY_KWH,
    ATTR_ENERGY_TOMORROW_KWH,
    ATTR_FORECASTS,
    ATTR_INTRADAY_SCALING,
    ATTR_TOTAL_SAMPLES,
    CONF_EXCLUDE_FROM_RECORDING,
)
from custom_components.electricitypricelevels.sensor.solarforecastsensor import (
    SolarForecastSensor,
)


def _make_sensor(
    *,
    exclude_from_recording: bool = True,
    forecast: list[dict] | None = None,
    total_samples: int = 0,
    data_since: str | None = None,
    intraday_scaling: float = 1.0,
) -> SolarForecastSensor:
    """Create a solar forecast sensor with a lightweight coordinator stub."""
    coordinator = SimpleNamespace(
        forecast=forecast or [],
        total_samples=total_samples,
        data_since=data_since,
        intraday_scaling=intraday_scaling,
        _local_tz=ZoneInfo("Europe/Stockholm"),
        register_update_callback=MagicMock(),
        unregister_update_callback=MagicMock(),
    )
    entry = MagicMock()
    entry.entry_id = "entry-id"
    entry.options = {CONF_EXCLUDE_FROM_RECORDING: exclude_from_recording}
    return SolarForecastSensor(MagicMock(), entry, {}, coordinator)


def test_sensor_uses_suggested_object_id_and_excludes_large_attribute():
    """The entity should rely on HA naming and keep forecasts out of recorder attrs."""
    sensor = _make_sensor(exclude_from_recording=False)

    assert sensor._attr_suggested_object_id == "solarforecast"
    assert sensor._attr_exclude_from_recording is False
    assert ATTR_FORECASTS in sensor._unrecorded_attributes


def test_sensor_recomputes_cached_state_and_summary_attributes():
    """The cached state should match the coordinator forecast payload."""
    local_tz = ZoneInfo("Europe/Stockholm")
    now_local = datetime.now(local_tz).replace(second=0, microsecond=0)
    forecast = [
        {
            "end": (now_local + timedelta(minutes=15)).isoformat(timespec="minutes"),
            "pow": 1.25,
            "raw": 1.00,
        },
        {
            "end": (now_local + timedelta(minutes=30)).isoformat(timespec="minutes"),
            "pow": 0.75,
            "raw": 0.60,
        },
        {
            "end": (now_local + timedelta(days=1, minutes=15)).isoformat(
                timespec="minutes"
            ),
            "pow": 2.00,
            "raw": 1.80,
        },
    ]
    sensor = _make_sensor(
        forecast=forecast,
        total_samples=12,
        data_since="2024-06-01",
        intraday_scaling=1.1234,
    )

    sensor._recompute_cache()
    attrs = sensor.extra_state_attributes

    expected_today = round(
        sum(
            entry["pow"] * 0.25
            for entry in forecast
            if datetime.fromisoformat(entry["end"]).date() == now_local.date()
        ),
        2,
    )
    expected_tomorrow = round(
        sum(
            entry["pow"] * 0.25
            for entry in forecast
            if datetime.fromisoformat(entry["end"]).date() > now_local.date()
        ),
        2,
    )

    assert sensor.native_value == 1.25
    assert attrs[ATTR_FORECASTS] == forecast
    assert attrs[ATTR_ENERGY_TODAY_KWH] == expected_today
    assert attrs[ATTR_ENERGY_TOMORROW_KWH] == expected_tomorrow
    assert attrs[ATTR_TOTAL_SAMPLES] == 12
    assert attrs[ATTR_DATA_SINCE] == "2024-06-01"
    assert attrs[ATTR_INTRADAY_SCALING] == 1.123
