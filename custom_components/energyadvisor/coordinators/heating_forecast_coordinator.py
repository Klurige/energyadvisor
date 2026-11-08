"""Coordinator for the heating forecast sensor.

Training data comes from the recorder's hourly long-term statistics, so the
model can learn from earlier winters immediately:

* heating power (``central_heating_power_entity``; power mean or energy change),
* outdoor temperature (``outdoor_temperature_entity``),
* wind speed (``wind_speed_entity``).

The weather entity (``weather_entity``) provides the hourly forecast through
``weather.get_forecasts``. Its current temperature and wind are also captured
every few minutes; this fills hours without sensor statistics and calibrates
the forecast against the local sensors (temperature offset, wind scale).

See ``heating_model`` for the model itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import (
    EnergyConverter,
    PowerConverter,
    SpeedConverter,
    TemperatureConverter,
)

from ..const import (
    CONF_CENTRAL_HEATING_POWER_ENTITY,
    CONF_CENTRAL_HEATING_POWER_W,
    CONF_OUTDOOR_TEMPERATURE_ENTITY,
    CONF_WEATHER_ENTITY,
    CONF_WIND_SPEED_ENTITY,
)
from ..heating_model import (
    HeatingModel,
    day_type_for,
    fallback_model,
    fit_heating_model,
    smooth_temperatures,
)

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
SLOT_MINUTES = 15
FORECAST_HOURS = 48
FORECAST_SLOT_COUNT = int(FORECAST_HOURS * 60 / SLOT_MINUTES)
HOUR = timedelta(hours=1)
TRAINING_WINDOW_DAYS = 365
RECENT_STATISTICS_DAYS = 2
WARMUP_HOURS = 7 * 24
RETRAIN_INTERVAL = timedelta(hours=6)
FORECAST_FETCH_INTERVAL = timedelta(minutes=30)
WEATHER_CAPTURE_INTERVAL = timedelta(minutes=10)
WEATHER_HOURS_RETENTION_DAYS = 365
STORE_SAVE_DELAY_SECONDS = 120
CALIBRATION_WINDOW_DAYS = 14
CALIBRATION_MIN_HOURS = 24
MAX_TEMPERATURE_OFFSET_C = 5.0
WIND_SCALE_MIN = 0.1
WIND_SCALE_MAX = 3.0
RESIDUAL_HOURS = 3
RESIDUAL_DECAY_HOURS = 6.0
RESIDUAL_MAX_KW = 3.0
MAX_PLAUSIBLE_HEATING_KW = 50.0

QUALITY_STATUS_OK = "ok"
QUALITY_STATUS_DEGRADED = "degraded"
QUALITY_STATUS_FALLBACK = "fallback"
WARNING_HEATING_STATISTICS_MISSING = "heating_statistics_missing"
WARNING_TEMPERATURE_STATISTICS_MISSING = "temperature_statistics_missing"
WARNING_WIND_STATISTICS_MISSING = "wind_statistics_missing"
WARNING_INSUFFICIENT_HISTORY = "insufficient_history"
WARNING_WEATHER_FORECAST_MISSING = "weather_forecast_missing"
WARNING_WEATHER_FORECAST_DAILY = "weather_forecast_daily_only"
WARNING_RECORDER_UNAVAILABLE = "recorder_unavailable"

FORECAST_SOURCE_HOURLY = "hourly"
FORECAST_SOURCE_DAILY = "daily"
FORECAST_SOURCE_NONE = "none"

_REQUESTED_UNITS = {
    PowerConverter.UNIT_CLASS: UnitOfPower.KILO_WATT,
    EnergyConverter.UNIT_CLASS: UnitOfEnergy.KILO_WATT_HOUR,
    TemperatureConverter.UNIT_CLASS: UnitOfTemperature.CELSIUS,
    SpeedConverter.UNIT_CLASS: UnitOfSpeed.METERS_PER_SECOND,
}


@dataclass(slots=True)
class HourlyObservation:
    """Hourly means from the recorder statistics."""

    heating_kw: float | None = None
    temp_c: float | None = None
    wind_ms: float | None = None


@dataclass(slots=True)
class Calibration:
    """Map weather-entity values onto the local sensors."""

    temperature_offset_c: float = 0.0
    wind_scale: float = 1.0


@dataclass(slots=True)
class _WeatherHour:
    temp_sum: float = 0.0
    temp_count: int = 0
    wind_sum: float = 0.0
    wind_count: int = 0

    @property
    def temp_c(self) -> float | None:
        return self.temp_sum / self.temp_count if self.temp_count else None

    @property
    def wind_ms(self) -> float | None:
        return self.wind_sum / self.wind_count if self.wind_count else None


@dataclass(slots=True)
class _StatisticsResult:
    observations: dict[datetime, HourlyObservation] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def floor_hour(timestamp: datetime) -> datetime:
    """Floor an aware timestamp to the start of its UTC hour."""
    return dt_util.as_utc(timestamp).replace(minute=0, second=0, microsecond=0)


def convert_temperature(value: Any, unit: str | None) -> float | None:
    """Convert a temperature to °C; unknown units are taken as °C."""
    number = _finite_float(value)
    if number is None:
        return None
    if unit and unit != UnitOfTemperature.CELSIUS:
        if unit not in TemperatureConverter.VALID_UNITS:
            return None
        number = TemperatureConverter.convert(number, unit, UnitOfTemperature.CELSIUS)
    return number


def convert_wind_speed(value: Any, unit: str | None) -> float | None:
    """Convert a wind speed to m/s; unknown units are rejected."""
    number = _finite_float(value)
    if number is None or number < 0.0:
        return None
    if not unit:
        return None
    if unit == UnitOfSpeed.METERS_PER_SECOND:
        return number
    if unit not in SpeedConverter.VALID_UNITS:
        return None
    return SpeedConverter.convert(number, unit, UnitOfSpeed.METERS_PER_SECOND)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_weather_forecast(
    forecast: list[Mapping[str, Any]],
    temperature_unit: str | None,
    wind_speed_unit: str | None,
    *,
    daily: bool = False,
) -> dict[datetime, tuple[float, float | None]]:
    """Convert ``weather.get_forecasts`` items into hourly UTC (°C, m/s) values.

    Daily items are spread over the 24 hours they cover using the mean of the
    high and low temperature.
    """
    hourly: dict[datetime, tuple[float, float | None]] = {}
    for item in forecast:
        when = dt_util.parse_datetime(str(item.get("datetime", "")))
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt_util.get_default_time_zone())
        temp = convert_temperature(item.get("temperature"), temperature_unit)
        if temp is None:
            continue
        wind = convert_wind_speed(item.get("wind_speed"), wind_speed_unit)
        if not daily:
            hourly[floor_hour(when)] = (temp, wind)
            continue
        low = convert_temperature(item.get("templow"), temperature_unit)
        mean_temp = (temp + low) / 2.0 if low is not None else temp
        start = floor_hour(when)
        for offset in range(24):
            hourly.setdefault(start + offset * HOUR, (mean_temp, wind))
    return hourly


def compute_calibration(
    observations: Mapping[datetime, HourlyObservation],
    weather_hours: Mapping[datetime, tuple[float | None, float | None]],
    now_utc: datetime,
) -> Calibration:
    """Learn how the weather entity relates to the local sensors."""
    since = now_utc - timedelta(days=CALIBRATION_WINDOW_DAYS)
    temp_diffs: list[float] = []
    wind_sensor_sum = 0.0
    wind_weather_sum = 0.0
    wind_pairs = 0
    for hour, (weather_temp, weather_wind) in weather_hours.items():
        if hour < since:
            continue
        observation = observations.get(hour)
        if observation is None:
            continue
        if observation.temp_c is not None and weather_temp is not None:
            temp_diffs.append(observation.temp_c - weather_temp)
        if observation.wind_ms is not None and weather_wind is not None:
            wind_sensor_sum += observation.wind_ms
            wind_weather_sum += weather_wind
            wind_pairs += 1
    calibration = Calibration()
    if len(temp_diffs) >= CALIBRATION_MIN_HOURS:
        offset = sum(temp_diffs) / len(temp_diffs)
        calibration.temperature_offset_c = round(
            max(-MAX_TEMPERATURE_OFFSET_C, min(MAX_TEMPERATURE_OFFSET_C, offset)), 2
        )
    if wind_pairs >= CALIBRATION_MIN_HOURS and wind_weather_sum > 0.0:
        scale = wind_sensor_sum / wind_weather_sum
        calibration.wind_scale = round(
            max(WIND_SCALE_MIN, min(WIND_SCALE_MAX, scale)), 3
        )
    return calibration


def build_weather_series(
    hours: list[datetime],
    observations: Mapping[datetime, HourlyObservation],
    weather_hours: Mapping[datetime, tuple[float | None, float | None]],
    forecast: Mapping[datetime, tuple[float, float | None]],
    calibration: Calibration,
    fill_from: datetime | None = None,
) -> tuple[list[float | None], list[float | None]]:
    """Return temperature and wind per hour, preferring the local sensors.

    Order: sensor statistics, captured weather-entity values, weather
    forecast. Weather-entity values are calibrated to the local sensors.
    From ``fill_from`` onwards, hours still without data repeat the last
    known value so the forecast covers the full horizon.
    """
    temps: list[float | None] = []
    winds: list[float | None] = []
    last_temp: float | None = None
    last_wind: float | None = None
    for hour in hours:
        observation = observations.get(hour)
        temp = observation.temp_c if observation is not None else None
        wind = observation.wind_ms if observation is not None else None
        for weather_temp, weather_wind in (
            weather_hours.get(hour, (None, None)),
            forecast.get(hour, (None, None)),
        ):
            if temp is None and weather_temp is not None:
                temp = weather_temp + calibration.temperature_offset_c
            if wind is None and weather_wind is not None:
                wind = weather_wind * calibration.wind_scale
        if fill_from is not None and hour >= fill_from:
            temp = last_temp if temp is None else temp
            wind = last_wind if wind is None else wind
        if temp is not None:
            last_temp = temp
        if wind is not None:
            last_wind = wind
        temps.append(temp)
        winds.append(wind)
    return temps, winds


class HeatingForecastCoordinator:
    """Train the heating model and publish a 48-hour heating forecast."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store = Store(
            hass, STORE_VERSION, f"energyadvisor_heating_forecast_{entry.entry_id}"
        )
        self._listeners: list[Callable[[], None]] = []
        self._update_callbacks: list[Callable[[], None]] = []
        self._pending_tasks: set[asyncio.Task[None]] = set()
        self._refresh_lock = asyncio.Lock()
        self._stopping = False

        self._observations: dict[datetime, HourlyObservation] = {}
        self._weather_hours: dict[datetime, _WeatherHour] = {}
        self._weather_forecast: dict[datetime, tuple[float, float | None]] = {}
        self._forecast_source = FORECAST_SOURCE_NONE
        self._calibration = Calibration()
        self._model: HeatingModel | None = None
        self._trained_at: datetime | None = None
        self._forecast_fetched_at: datetime | None = None
        self._statistics_warnings: list[str] = []
        self._model_warnings: list[str] = []
        self._residual_kw = 0.0
        self._forecast_slots: list[dict[str, object]] = []
        self._forecast_generated_at: datetime | None = None

    # -- Config ------------------------------------------------------------

    def _option_entity(self, key: str) -> str:
        value = self.entry.options.get(key)
        return value if isinstance(value, str) else ""

    @property
    def _heating_entity(self) -> str:
        return self._option_entity(CONF_CENTRAL_HEATING_POWER_ENTITY)

    @property
    def _temperature_entity(self) -> str:
        return self._option_entity(CONF_OUTDOOR_TEMPERATURE_ENTITY)

    @property
    def _wind_entity(self) -> str:
        return self._option_entity(CONF_WIND_SPEED_ENTITY)

    @property
    def _weather_entity(self) -> str:
        return self._option_entity(CONF_WEATHER_ENTITY)

    @property
    def _rated_power_kw(self) -> float | None:
        value = _finite_float(self.entry.options.get(CONF_CENTRAL_HEATING_POWER_W))
        return value / 1000.0 if value is not None and value > 0.0 else None

    # -- Public values -----------------------------------------------------

    @property
    def model(self) -> HeatingModel:
        """Return the active model (fitted, or the rated-power fallback)."""
        if self._model is not None:
            return self._model
        return fallback_model(self._rated_power_kw)

    @property
    def forecast_slots(self) -> list[dict[str, object]]:
        """Return the 15-minute heating slots anchored to local midnight."""
        return [dict(slot) for slot in self._forecast_slots]

    @property
    def current_heating_kw(self) -> float | None:
        """Return the forecast for the slot covering now."""
        local_now = dt_util.now()
        index = (local_now.hour * 60 + local_now.minute) // SLOT_MINUTES
        if 0 <= index < len(self._forecast_slots):
            return float(self._forecast_slots[index]["load"])  # type: ignore[arg-type]
        return None

    def energy_for_local_date_kwh(self, day_offset: int) -> float | None:
        """Return the forecast energy for today (0) or tomorrow (1)."""
        if not self._forecast_slots:
            return None
        target = (dt_util.now() + timedelta(days=day_offset)).date().isoformat()
        total = sum(
            float(slot["load"]) * SLOT_MINUTES / 60.0  # type: ignore[arg-type]
            for slot in self._forecast_slots
            if str(slot["from"]).startswith(target)
        )
        return round(total, 2)

    @property
    def quality_status(self) -> str:
        """Return ok, degraded or fallback."""
        if self._model is None or not self._model.fitted:
            return QUALITY_STATUS_FALLBACK
        if self._forecast_source != FORECAST_SOURCE_HOURLY:
            return QUALITY_STATUS_DEGRADED
        return QUALITY_STATUS_OK

    @property
    def quality_warnings(self) -> list[str]:
        """Return machine-readable warnings."""
        warnings = [*self._statistics_warnings, *self._model_warnings]
        if self._forecast_source == FORECAST_SOURCE_NONE:
            warnings.append(WARNING_WEATHER_FORECAST_MISSING)
        elif self._forecast_source == FORECAST_SOURCE_DAILY:
            warnings.append(WARNING_WEATHER_FORECAST_DAILY)
        return list(dict.fromkeys(warnings))

    @property
    def reason(self) -> str:
        """Return a human-readable status."""
        model = self.model
        if not model.fitted:
            if model.slope_kw_per_k > 0.0:
                return (
                    "Too little heating history; using a straight line from 0 kW "
                    "at 15 °C to the rated heating power at -15 °C."
                )
            return (
                "Too little heating history and no rated heating power; "
                "the forecast is zero."
            )
        if self._forecast_source == FORECAST_SOURCE_NONE:
            return (
                "Model trained; no weather forecast, so the latest weather is repeated."
            )
        return "Model trained on recorder statistics."

    @property
    def diagnostics(self) -> dict[str, object]:
        """Return model and calibration details for the sensor attributes."""
        return {
            "model": self.model.as_dict(),
            "last_trained": (
                dt_util.as_local(self._trained_at).isoformat(timespec="minutes")
                if self._trained_at is not None
                else None
            ),
            "last_forecast_generation": (
                dt_util.as_local(self._forecast_generated_at).isoformat(
                    timespec="minutes"
                )
                if self._forecast_generated_at is not None
                else None
            ),
            "forecast_source": self._forecast_source,
            "temperature_offset_c": self._calibration.temperature_offset_c,
            "wind_scale": self._calibration.wind_scale,
            "residual_correction_kw": round(self._residual_kw, 3),
        }

    def register_update_callback(self, cb: Callable[[], None]) -> None:
        """Register a callback for forecast updates."""
        self._update_callbacks.append(cb)

    def unregister_update_callback(self, cb: Callable[[], None]) -> None:
        """Remove a callback registered with ``register_update_callback``."""
        try:
            self._update_callbacks.remove(cb)
        except ValueError:
            pass

    # -- Lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        """Restore state, publish a first forecast and start background work."""
        await self._async_load_store()
        self._capture_weather_sample(dt_util.utcnow())
        self._recompute_forecast(dt_util.utcnow())
        self._listeners.append(
            async_track_time_interval(
                self.hass, self._handle_weather_capture, WEATHER_CAPTURE_INTERVAL
            )
        )
        self._listeners.append(
            async_track_time_change(
                self.hass,
                self._handle_quarter_hour,
                minute=[0, 15, 30, 45],
                second=5,
            )
        )
        self._spawn(self.async_refresh(), "energyadvisor heating forecast startup")

    async def async_shutdown(self) -> None:
        """Stop listeners and wait for running work."""
        self._stopping = True
        for remove in self._listeners:
            remove()
        self._listeners.clear()
        tasks = set(self._pending_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._weather_hours or self._model is not None:
            await self._store.async_save(self._store_data())

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        if self._stopping:
            coro.close()
            return
        task = self.hass.async_create_background_task(coro, name)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    @callback
    def _handle_weather_capture(self, now: datetime) -> None:
        self._capture_weather_sample(dt_util.as_utc(now))
        self._store.async_delay_save(self._store_data, STORE_SAVE_DELAY_SECONDS)

    @callback
    def _handle_quarter_hour(self, now: datetime) -> None:
        self._spawn(
            self.async_refresh(dt_util.as_utc(now)),
            "energyadvisor heating forecast refresh",
        )

    def _notify_update(self) -> None:
        if self._stopping:
            return
        for cb in tuple(self._update_callbacks):
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error notifying heating forecast listeners")

    # -- Refresh -------------------------------------------------------------

    async def async_refresh(self, now_utc: datetime | None = None) -> None:
        """Reload data that is due, retrain if due, and republish the forecast."""
        now_utc = now_utc or dt_util.utcnow()
        async with self._refresh_lock:
            retrain = (
                self._trained_at is None
                or now_utc - self._trained_at >= RETRAIN_INTERVAL
            )
            if retrain:
                await self._async_reload_statistics(
                    now_utc - timedelta(days=TRAINING_WINDOW_DAYS), now_utc, True
                )
                self._calibration = compute_calibration(
                    self._observations, self._weather_hour_values(), now_utc
                )
                await self._async_train(now_utc)
            elif self._statistics_are_behind(now_utc):
                await self._async_reload_statistics(
                    now_utc - timedelta(days=RECENT_STATISTICS_DAYS), now_utc, False
                )
            if (
                self._forecast_fetched_at is None
                or now_utc - self._forecast_fetched_at >= FORECAST_FETCH_INTERVAL
            ):
                await self._async_fetch_weather_forecast(now_utc)
            self._recompute_forecast(now_utc)
        self._notify_update()

    def _statistics_are_behind(self, now_utc: datetime) -> bool:
        latest = max(self._observations, default=None)
        return latest is None or latest < floor_hour(now_utc) - HOUR

    async def _async_reload_statistics(
        self, start_utc: datetime, end_utc: datetime, replace: bool
    ) -> None:
        if not self._heating_entity:
            return
        try:
            from homeassistant.components.recorder import get_instance

            instance = get_instance(self.hass)
        except (ImportError, KeyError):
            self._statistics_warnings = [WARNING_RECORDER_UNAVAILABLE]
            return
        try:
            result = await instance.async_add_executor_job(
                self._load_statistics_sync, start_utc, end_utc
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not read heating statistics from the recorder")
            self._statistics_warnings = [WARNING_RECORDER_UNAVAILABLE]
            return
        self._statistics_warnings = result.warnings
        if replace:
            self._observations = result.observations
        else:
            self._observations.update(result.observations)
        oldest = end_utc - timedelta(days=TRAINING_WINDOW_DAYS)
        for hour in [hour for hour in self._observations if hour < oldest]:
            del self._observations[hour]

    def _load_statistics_sync(
        self, start_utc: datetime, end_utc: datetime
    ) -> _StatisticsResult:
        """Read hourly statistics for the configured sensors (recorder thread)."""
        from homeassistant.components.recorder.statistics import (
            get_metadata,
            statistics_during_period,
        )

        result = _StatisticsResult()
        roles = {
            "heating": self._heating_entity,
            "temp": self._temperature_entity,
            "wind": self._wind_entity,
        }
        statistic_ids = {entity for entity in roles.values() if entity}
        metadata = get_metadata(self.hass, statistic_ids=statistic_ids)

        heating_field: str | None = None
        heating_meta = metadata.get(roles["heating"])
        if heating_meta is not None:
            unit = heating_meta[1].get("unit_of_measurement")
            if heating_meta[1].get("has_mean") and unit in PowerConverter.VALID_UNITS:
                heating_field = "mean"
            elif heating_meta[1].get("has_sum") and unit in EnergyConverter.VALID_UNITS:
                heating_field = "change"
        if heating_field is None:
            result.warnings.append(WARNING_HEATING_STATISTICS_MISSING)
            return result

        valid_ids = {roles["heating"]}
        for role, converter, warning in (
            ("temp", TemperatureConverter, WARNING_TEMPERATURE_STATISTICS_MISSING),
            ("wind", SpeedConverter, WARNING_WIND_STATISTICS_MISSING),
        ):
            entity = roles[role]
            if not entity:
                continue
            meta = metadata.get(entity)
            if (
                meta is not None
                and meta[1].get("has_mean")
                and meta[1].get("unit_of_measurement") in converter.VALID_UNITS
            ):
                valid_ids.add(entity)
            else:
                result.warnings.append(warning)

        stats = statistics_during_period(
            self.hass,
            start_utc,
            end_utc,
            valid_ids,
            "hour",
            _REQUESTED_UNITS,
            {"mean", "change"},
        )

        def rows_for(entity: str, field_name: str):
            for row in stats.get(entity, []) if entity in valid_ids else []:
                start = row.get("start")
                if isinstance(start, (int, float)):
                    hour = datetime.fromtimestamp(start, timezone.utc)
                elif isinstance(start, datetime):
                    hour = dt_util.as_utc(start)
                else:
                    continue
                value = _finite_float(row.get(field_name))
                if value is not None:
                    yield floor_hour(hour), value

        observations = result.observations
        for hour, value in rows_for(roles["heating"], heating_field):
            if 0.0 <= value <= MAX_PLAUSIBLE_HEATING_KW:
                observations.setdefault(hour, HourlyObservation()).heating_kw = value
        for hour, value in rows_for(roles["temp"], "mean"):
            observations.setdefault(hour, HourlyObservation()).temp_c = value
        for hour, value in rows_for(roles["wind"], "mean"):
            if value >= 0.0:
                observations.setdefault(hour, HourlyObservation()).wind_ms = value
        return result

    async def _async_train(self, now_utc: datetime) -> None:
        last_hour = floor_hour(now_utc) - HOUR
        first_hour = min(self._observations, default=last_hour)
        hours = _hour_range(first_hour, last_hour)
        temps, winds = build_weather_series(
            hours,
            self._observations,
            self._weather_hour_values(),
            {},
            self._calibration,
        )
        heating = [
            (
                self._observations[hour].heating_kw
                if hour in self._observations
                else None
            )
            for hour in hours
        ]
        local_hours, day_types = _local_calendar(hours)
        ages = [(now_utc - hour).total_seconds() / 86400.0 for hour in hours]
        try:
            model = await self.hass.async_add_executor_job(
                fit_heating_model, temps, winds, heating, local_hours, day_types, ages
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Heating model training failed")
            model = None
        self._trained_at = now_utc
        if model is None:
            self._model = None
            self._model_warnings = [WARNING_INSUFFICIENT_HISTORY]
        else:
            self._model = model
            self._model_warnings = []
            _LOGGER.debug("Heating model trained: %s", model.as_dict())
        self._store.async_delay_save(self._store_data, STORE_SAVE_DELAY_SECONDS)

    async def _async_fetch_weather_forecast(self, now_utc: datetime) -> None:
        entity_id = self._weather_entity
        self._forecast_fetched_at = now_utc
        if not entity_id:
            self._weather_forecast = {}
            self._forecast_source = FORECAST_SOURCE_NONE
            return
        state = self.hass.states.get(entity_id)
        attributes = state.attributes if state is not None else {}
        temperature_unit = attributes.get("temperature_unit")
        wind_speed_unit = attributes.get("wind_speed_unit")
        for forecast_type, source in (
            ("hourly", FORECAST_SOURCE_HOURLY),
            ("daily", FORECAST_SOURCE_DAILY),
        ):
            try:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"type": forecast_type},
                    target={"entity_id": entity_id},
                    blocking=True,
                    return_response=True,
                )
            except (HomeAssistantError, ValueError) as err:
                _LOGGER.debug(
                    "%s forecast from %s is unavailable: %s",
                    forecast_type,
                    entity_id,
                    err,
                )
                continue
            items = ((response or {}).get(entity_id) or {}).get("forecast") or []
            parsed = parse_weather_forecast(
                items,
                temperature_unit,
                wind_speed_unit,
                daily=source == FORECAST_SOURCE_DAILY,
            )
            if parsed:
                self._weather_forecast = parsed
                self._forecast_source = source
                return
        # Keep an earlier forecast while it still covers the future.
        if not any(hour >= floor_hour(now_utc) for hour in self._weather_forecast):
            self._weather_forecast = {}
            self._forecast_source = FORECAST_SOURCE_NONE

    # -- Weather capture -------------------------------------------------------

    def _capture_weather_sample(self, now_utc: datetime) -> None:
        entity_id = self._weather_entity
        if not entity_id:
            return
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        temp = convert_temperature(
            state.attributes.get("temperature"),
            state.attributes.get("temperature_unit"),
        )
        wind = convert_wind_speed(
            state.attributes.get("wind_speed"),
            state.attributes.get("wind_speed_unit"),
        )
        if temp is None and wind is None:
            return
        bucket = self._weather_hours.setdefault(floor_hour(now_utc), _WeatherHour())
        if temp is not None:
            bucket.temp_sum += temp
            bucket.temp_count += 1
        if wind is not None:
            bucket.wind_sum += wind
            bucket.wind_count += 1
        oldest = now_utc - timedelta(days=WEATHER_HOURS_RETENTION_DAYS)
        for hour in [hour for hour in self._weather_hours if hour < oldest]:
            del self._weather_hours[hour]

    def _weather_hour_values(self) -> dict[datetime, tuple[float | None, float | None]]:
        return {
            hour: (bucket.temp_c, bucket.wind_ms)
            for hour, bucket in self._weather_hours.items()
        }

    # -- Forecast ----------------------------------------------------------------

    def _recompute_forecast(self, now_utc: datetime) -> None:
        """Build the 192-slot forecast from the model and weather series."""
        model = self.model
        local_now = dt_util.as_local(now_utc)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        slot_starts_local = [
            start_local + timedelta(minutes=SLOT_MINUTES * index)
            for index in range(FORECAST_SLOT_COUNT)
        ]
        first_slot_hour = floor_hour(slot_starts_local[0])
        last_slot_hour = floor_hour(slot_starts_local[-1])
        hours = _hour_range(first_slot_hour - WARMUP_HOURS * HOUR, last_slot_hour)
        current_hour = floor_hour(now_utc)
        weather_hours = self._weather_hour_values()
        temps, winds = build_weather_series(
            hours,
            self._observations,
            weather_hours,
            self._weather_forecast,
            self._calibration,
            fill_from=current_hour,
        )
        smoothed = smooth_temperatures(temps, model.time_constant_h)
        local_hours, day_types = _local_calendar(hours)
        predictions: dict[datetime, float | None] = {}
        for index, hour in enumerate(hours):
            temp_eff = smoothed[index]
            predictions[hour] = (
                None
                if temp_eff is None
                else model.predict_kw(
                    temp_eff, winds[index], local_hours[index], day_types[index]
                )
            )

        self._residual_kw = self._residual_correction(predictions, current_hour)
        slots: list[dict[str, object]] = []
        for slot_start in slot_starts_local:
            hour = floor_hour(slot_start)
            value = predictions.get(hour)
            if value is not None and self._residual_kw and hour >= current_hour:
                hours_ahead = (hour - current_hour) / HOUR
                value += self._residual_kw * math.exp(
                    -hours_ahead / RESIDUAL_DECAY_HOURS
                )
            slots.append(
                {
                    "from": slot_start.strftime("%Y-%m-%dT%H:%M"),
                    "load": round(max(0.0, value or 0.0), 3),
                }
            )
        self._forecast_slots = slots
        self._forecast_generated_at = now_utc

    def _residual_correction(
        self, predictions: Mapping[datetime, float | None], current_hour: datetime
    ) -> float:
        """Return how far the latest complete hours sat above the model."""
        if self._model is None or not self._model.fitted:
            return 0.0
        errors: list[float] = []
        for offset in range(1, RESIDUAL_HOURS + 1):
            hour = current_hour - offset * HOUR
            observation = self._observations.get(hour)
            predicted = predictions.get(hour)
            if (
                observation is None
                or observation.heating_kw is None
                or predicted is None
            ):
                return 0.0
            errors.append(observation.heating_kw - predicted)
        residual = sum(errors) / len(errors)
        return max(-RESIDUAL_MAX_KW, min(RESIDUAL_MAX_KW, residual))

    # -- Storage -----------------------------------------------------------------

    def _store_data(self) -> dict[str, Any]:
        return {
            "model": self._model.as_dict() if self._model is not None else None,
            "trained_at": (
                self._trained_at.isoformat() if self._trained_at is not None else None
            ),
            "calibration": {
                "temperature_offset_c": self._calibration.temperature_offset_c,
                "wind_scale": self._calibration.wind_scale,
            },
            "weather_hours": {
                hour.isoformat(): [
                    round(bucket.temp_sum, 3),
                    bucket.temp_count,
                    round(bucket.wind_sum, 3),
                    bucket.wind_count,
                ]
                for hour, bucket in self._weather_hours.items()
            },
        }

    async def _async_load_store(self) -> None:
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        model_data = data.get("model")
        if isinstance(model_data, dict):
            self._model = HeatingModel.from_dict(model_data)
        # The stored model is used until the startup retrain finishes; the
        # retrain itself is always run so new history is picked up.
        calibration = data.get("calibration")
        if isinstance(calibration, dict):
            self._calibration = Calibration(
                temperature_offset_c=_finite_float(
                    calibration.get("temperature_offset_c")
                )
                or 0.0,
                wind_scale=_finite_float(calibration.get("wind_scale")) or 1.0,
            )
        weather_hours = data.get("weather_hours")
        if isinstance(weather_hours, dict):
            for key, values in weather_hours.items():
                when = dt_util.parse_datetime(str(key))
                if when is None or not isinstance(values, list) or len(values) != 4:
                    continue
                try:
                    self._weather_hours[floor_hour(when)] = _WeatherHour(
                        float(values[0]),
                        int(values[1]),
                        float(values[2]),
                        int(values[3]),
                    )
                except (TypeError, ValueError):
                    continue


def _hour_range(first: datetime, last: datetime) -> list[datetime]:
    count = int((last - first) / HOUR) + 1
    return [first + index * HOUR for index in range(max(0, count))]


def _local_calendar(hours: list[datetime]) -> tuple[list[int], list[str]]:
    local_hours: list[int] = []
    day_types: list[str] = []
    for hour in hours:
        local = dt_util.as_local(hour)
        local_hours.append(local.hour)
        day_types.append(day_type_for(local.weekday()))
    return local_hours, day_types
