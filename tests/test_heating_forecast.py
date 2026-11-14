"""Tests for the heating model and heating forecast coordinator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import random
from unittest.mock import MagicMock, patch

from homeassistant.core import SupportsResponse
from homeassistant.util import dt as dt_util
import pytest

from custom_components.energyadvisor.const import (
    CONF_CENTRAL_HEATING_POWER_ENTITY,
    CONF_CENTRAL_HEATING_POWER_W,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_WEATHER_ENTITY,
    CONF_WIND_SPEED_ENTITY,
)
from custom_components.energyadvisor.coordinators.heating_forecast_coordinator import (
    FORECAST_SLOT_COUNT,
    QUALITY_STATUS_FALLBACK,
    QUALITY_STATUS_OK,
    WARNING_HEATING_STATISTICS_MISSING,
    WARNING_WIND_STATISTICS_MISSING,
    Calibration,
    HeatingForecastCoordinator,
    HourlyObservation,
    build_weather_series,
    compute_calibration,
    floor_hour,
    parse_weather_forecast,
)
from custom_components.energyadvisor.heating_model import (
    HeatingModel,
    day_type_for,
    fallback_model,
    fit_heating_model,
    smooth_temperatures,
)

UTC = timezone.utc


def _synthetic_series(days: int, *, seed: int = 3):
    """Heat pump with backup: balance 15 °C, backup knee -6 °C, tau 12 h."""
    rng = random.Random(seed)
    temps, winds, heating, hours, day_types, ages = [], [], [], [], [], []
    alpha = 1 - math.exp(-1 / 12)
    state = None
    count = days * 24
    for index in range(count):
        day = index / 24
        temp = (
            2
            + 12 * math.sin(2 * math.pi * (day - 100) / 365)
            + 4 * math.sin(2 * math.pi * ((index % 24) - 9) / 24)
            + rng.gauss(0, 3)
        )
        wind = max(0.0, rng.gauss(4, 2.5))
        state = temp if state is None else state + alpha * (temp - state)
        power = (
            0.05
            + (0.12 + 0.015 * wind) * max(0.0, 15 - state)
            + 0.25 * max(0.0, -6 - state)
        )
        temps.append(temp)
        winds.append(wind)
        heating.append(max(0.0, power + rng.gauss(0, 0.15)))
        hours.append(index % 24)
        day_types.append(day_type_for((index // 24) % 7))
        ages.append((count - index) / 24)
    return temps, winds, heating, hours, day_types, ages


def test_fit_recovers_balance_backup_wind_and_inertia():
    model = fit_heating_model(*_synthetic_series(240))

    assert model is not None and model.fitted
    assert model.balance_temp_c == pytest.approx(15, abs=1.0)
    assert model.backup_temp_c == pytest.approx(-6, abs=3.0)
    assert model.time_constant_h == 12.0
    assert model.uses_wind
    assert model.slope_kw_per_k == pytest.approx(0.12, rel=0.25)
    assert model.wind_kw_per_k_ms == pytest.approx(0.015, rel=0.3)
    assert model.backup_kw_per_k == pytest.approx(0.25, rel=0.3)
    assert model.r2 > 0.9
    # Wind raises the demand at the same temperature.
    assert model.base_kw_at(-5, 10) > model.base_kw_at(-5, 0)


def test_fit_without_wind_data_drops_wind_term():
    temps, _winds, heating, hours, day_types, ages = _synthetic_series(60)
    model = fit_heating_model(
        temps, [None] * len(temps), heating, hours, day_types, ages
    )

    assert model is not None
    assert not model.uses_wind
    assert model.wind_kw_per_k_ms == 0.0


def test_fit_returns_none_without_heating_history():
    count = 30 * 24
    model = fit_heating_model(
        [20.0] * count,
        [3.0] * count,
        [0.0] * count,
        [i % 24 for i in range(count)],
        ["workday"] * count,
        [float(count - i) / 24 for i in range(count)],
    )

    assert model is None


def test_smoothing_carries_short_gaps_and_restarts_after_long_gaps():
    smoothed = smooth_temperatures([0.0, None, 10.0], 0.0)
    assert smoothed == [0.0, 0.0, 10.0]

    series = [0.0] + [None] * 7 + [10.0]
    smoothed = smooth_temperatures(series, 12.0, max_gap_hours=6)
    assert smoothed[6] == 0.0
    assert smoothed[7] is None
    assert smoothed[8] == 10.0


def test_fallback_model_reaches_rated_power_at_design_temperature():
    model = fallback_model(6.0)

    assert not model.fitted
    assert model.base_kw_at(-15.0, None) == pytest.approx(6.0)
    assert model.base_kw_at(15.0, None) == 0.0
    assert fallback_model(None).base_kw_at(-15.0, None) == 0.0


def test_parse_weather_forecast_converts_units_and_spreads_daily_values():
    hourly = parse_weather_forecast(
        [
            {
                "datetime": "2026-01-10T12:00:00+00:00",
                "temperature": 32,
                "wind_speed": 36,
            },
            {"datetime": "bad", "temperature": 1},
            {"datetime": "2026-01-10T13:00:00+00:00", "temperature": None},
        ],
        "°F",
        "km/h",
    )
    assert hourly == {
        datetime(2026, 1, 10, 12, tzinfo=UTC): (pytest.approx(0.0), pytest.approx(10.0))
    }

    daily = parse_weather_forecast(
        [{"datetime": "2026-01-10T00:00:00+00:00", "temperature": 4, "templow": -2}],
        "°C",
        "m/s",
        daily=True,
    )
    assert len(daily) == 24
    assert daily[datetime(2026, 1, 10, 23, tzinfo=UTC)] == (1.0, None)


def test_calibration_learns_offset_and_wind_scale():
    now = datetime(2026, 1, 20, tzinfo=UTC)
    observations = {}
    weather = {}
    for index in range(48):
        hour = now - timedelta(hours=index + 1)
        observations[hour] = HourlyObservation(temp_c=-2.0, wind_ms=3.0)
        weather[hour] = (-3.5, 6.0)

    calibration = compute_calibration(observations, weather, now)

    assert calibration.temperature_offset_c == pytest.approx(1.5)
    assert calibration.wind_scale == pytest.approx(0.5)
    assert compute_calibration({}, weather, now) == Calibration()


def test_weather_series_prefers_sensors_and_fills_the_horizon():
    base = datetime(2026, 1, 10, tzinfo=UTC)
    hours = [base + timedelta(hours=i) for i in range(5)]
    observations = {hours[0]: HourlyObservation(temp_c=-1.0, wind_ms=2.0)}
    weather_hours = {hours[0]: (-9.0, 9.0), hours[1]: (-2.0, 4.0)}
    forecast = {hours[2]: (-4.0, 8.0)}
    calibration = Calibration(temperature_offset_c=1.0, wind_scale=0.5)

    temps, winds = build_weather_series(
        hours, observations, weather_hours, forecast, calibration, fill_from=hours[2]
    )

    assert temps == [-1.0, -1.0, -3.0, -3.0, -3.0]
    assert winds == [2.0, 2.0, 4.0, 4.0, 4.0]


def _entry(options):
    entry = MagicMock()
    entry.entry_id = "heating-entry"
    entry.options = options
    return entry


_FULL_OPTIONS = {
    CONF_CENTRAL_HEATING_POWER_ENTITY: "sensor.heat_pump_energy",
    CONF_OUTDOOR_TEMPERATURE_ENTITY: "sensor.outdoor",
    CONF_WIND_SPEED_ENTITY: "sensor.wind",
    CONF_WEATHER_ENTITY: "weather.home",
}


def test_statistics_use_energy_change_and_skip_unsupported_wind(hass):
    coordinator = HeatingForecastCoordinator(hass, _entry(_FULL_OPTIONS))
    hour = datetime(2026, 1, 10, 5, tzinfo=UTC)
    metadata = {
        "sensor.heat_pump_energy": (
            1,
            {"has_mean": False, "has_sum": True, "unit_of_measurement": "kWh"},
        ),
        "sensor.outdoor": (
            2,
            {"has_mean": True, "has_sum": False, "unit_of_measurement": "°C"},
        ),
        "sensor.wind": (
            3,
            {"has_mean": True, "has_sum": False, "unit_of_measurement": "parsec/h"},
        ),
    }
    stats = {
        "sensor.heat_pump_energy": [{"start": hour.timestamp(), "change": 1.8}],
        "sensor.outdoor": [{"start": hour.timestamp(), "mean": -4.0}],
    }

    with patch(
        "homeassistant.components.recorder.statistics.get_metadata",
        return_value=metadata,
    ), patch(
        "homeassistant.components.recorder.statistics.statistics_during_period",
        return_value=stats,
    ) as during_period:
        result = coordinator._load_statistics_sync(hour, hour + timedelta(hours=1))

    assert during_period.call_args.args[3] == {
        "sensor.heat_pump_energy",
        "sensor.outdoor",
    }
    assert result.observations[hour] == HourlyObservation(heating_kw=1.8, temp_c=-4.0)
    assert result.warnings == [WARNING_WIND_STATISTICS_MISSING]


def test_statistics_without_heating_metadata_warn(hass):
    coordinator = HeatingForecastCoordinator(hass, _entry(_FULL_OPTIONS))
    hour = datetime(2026, 1, 10, tzinfo=UTC)
    with patch(
        "homeassistant.components.recorder.statistics.get_metadata",
        return_value={},
    ):
        result = coordinator._load_statistics_sync(hour, hour)

    assert result.observations == {}
    assert result.warnings == [WARNING_HEATING_STATISTICS_MISSING]


def _set_weather_state(hass):
    hass.states.async_set(
        "weather.home",
        "cloudy",
        {
            "temperature": -3.0,
            "temperature_unit": "°C",
            "wind_speed": 18.0,
            "wind_speed_unit": "km/h",
        },
    )


def _register_forecast_service(hass, temp_c: float, wind_kmh: float):
    async def _handler(call):
        start = floor_hour(dt_util.utcnow()) - timedelta(hours=5)
        return {
            "weather.home": {
                "forecast": [
                    {
                        "datetime": (start + timedelta(hours=i)).isoformat(),
                        "temperature": temp_c,
                        "wind_speed": wind_kmh,
                    }
                    for i in range(60)
                ]
            }
        }

    hass.services.async_register(
        "weather", "get_forecasts", _handler, supports_response=SupportsResponse.ONLY
    )


async def test_forecast_uses_model_weather_forecast_and_residual(hass, freezer):
    hass.config.time_zone = "UTC"
    freezer.move_to(datetime(2026, 2, 2, 10, 0, tzinfo=UTC))
    _set_weather_state(hass)
    _register_forecast_service(hass, temp_c=-5.0, wind_kmh=36.0)
    coordinator = HeatingForecastCoordinator(hass, _entry(_FULL_OPTIONS))
    model = HeatingModel(
        balance_temp_c=15.0,
        backup_temp_c=None,
        time_constant_h=0.0,
        slope_kw_per_k=0.1,
        wind_kw_per_k_ms=0.01,
        uses_wind=True,
        fitted=True,
    )
    now = dt_util.utcnow()
    current_hour = floor_hour(now)
    coordinator._model = model
    coordinator._trained_at = now
    # The last three complete hours ran 0.6 kW above the model.
    for offset in range(1, 4):
        coordinator._observations[current_hour - timedelta(hours=offset)] = (
            HourlyObservation(heating_kw=20 * (0.1 + 0.01 * 10) + 0.6)
        )

    with patch.object(coordinator, "_async_reload_statistics") as reload:
        await coordinator.async_refresh(now)
    reload.assert_not_called()

    slots = coordinator.forecast_slots
    assert len(slots) == FORECAST_SLOT_COUNT
    expected_kw = 20 * (0.1 + 0.01 * 10)  # (15 - -5) K, 36 km/h = 10 m/s
    assert coordinator.quality_status == QUALITY_STATUS_OK
    assert coordinator.diagnostics["residual_correction_kw"] == pytest.approx(0.6)
    assert slots[-1]["load"] == pytest.approx(expected_kw, abs=0.01)
    assert coordinator.current_heating_kw == pytest.approx(expected_kw + 0.6, abs=0.01)
    tomorrow = (dt_util.now() + timedelta(days=1)).date().isoformat()
    tomorrow_slots = [slot for slot in slots if str(slot["from"]).startswith(tomorrow)]
    assert len(tomorrow_slots) == 96
    expected_energy = round(
        sum(float(slot["load"]) * 0.25 for slot in tomorrow_slots), 2
    )
    assert coordinator.energy_for_local_date_kwh(1) == pytest.approx(expected_energy)


async def test_cold_start_uses_rated_power_fallback(hass):
    _set_weather_state(hass)
    _register_forecast_service(hass, temp_c=-15.0, wind_kmh=0.0)
    options = {**_FULL_OPTIONS, CONF_CENTRAL_HEATING_POWER_W: 6000}
    coordinator = HeatingForecastCoordinator(hass, _entry(options))

    async def _no_statistics(*_args):
        coordinator._statistics_warnings = [WARNING_HEATING_STATISTICS_MISSING]

    with patch.object(
        coordinator, "_async_reload_statistics", side_effect=_no_statistics
    ):
        await coordinator.async_refresh(dt_util.utcnow())

    assert coordinator.quality_status == QUALITY_STATUS_FALLBACK
    assert WARNING_HEATING_STATISTICS_MISSING in coordinator.quality_warnings
    assert coordinator.forecast_slots[-1]["load"] == pytest.approx(6.0, abs=0.05)


async def test_weather_capture_survives_restart(hass):
    _set_weather_state(hass)
    coordinator = HeatingForecastCoordinator(hass, _entry(_FULL_OPTIONS))
    now = dt_util.utcnow()
    coordinator._capture_weather_sample(now)
    stored = coordinator._store_data()

    restored = HeatingForecastCoordinator(hass, _entry(_FULL_OPTIONS))
    with patch.object(restored._store, "async_load", return_value=stored):
        await restored._async_load_store()

    temp, wind = restored._weather_hour_values()[floor_hour(now)]
    assert temp == pytest.approx(-3.0)
    assert wind == pytest.approx(5.0)


@pytest.mark.parametrize(
    ("options", "created"),
    [
        (_FULL_OPTIONS, True),
        ({CONF_CENTRAL_HEATING_POWER_ENTITY: "sensor.heat_pump_energy"}, False),
        ({CONF_WEATHER_ENTITY: "weather.home"}, False),
    ],
)
async def test_sensor_platform_creates_heating_sensor_when_configured(options, created):
    from unittest.mock import AsyncMock

    from custom_components.energyadvisor.sensor import (
        async_setup_entry as sensor_async_setup_entry,
    )
    from custom_components.energyadvisor.sensor.heatingforecastsensor import (
        HeatingForecastSensor,
    )
    from tests.test_debug_mode import _entry as _platform_entry, _hass

    hass = _hass()
    entry = _platform_entry(**options)
    entry.async_on_unload = MagicMock()
    add_entities = MagicMock()
    coordinator = MagicMock()
    coordinator.async_setup = AsyncMock()
    with (
        patch(
            "custom_components.energyadvisor.sensor.async_get_integration",
            new=AsyncMock(return_value=MagicMock(version="1.0")),
        ),
        patch(
            "custom_components.energyadvisor.sensor._resolve_nordpool_config_entry_id",
            return_value="nordpool_entry",
        ),
        patch("custom_components.energyadvisor.sensor.NordpoolDataCoordinator"),
        patch(
            "custom_components.energyadvisor.sensor.HeatingForecastCoordinator",
            return_value=coordinator,
        ),
    ):
        await sensor_async_setup_entry(hass, entry, add_entities)

    entities = add_entities.call_args.args[0]
    sensors = [e for e in entities if isinstance(e, HeatingForecastSensor)]
    assert len(sensors) == (1 if created else 0)
    assert (entry.runtime_data.heating_coordinator is coordinator) is created
    assert coordinator.async_setup.await_count == (1 if created else 0)
