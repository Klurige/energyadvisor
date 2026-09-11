"""Coordinator for the household Load forecast sensor.

The coordinator stores raw meter history in SQLite and rebuilds a seasonal
baseline forecast from retained slot rows once enough history is available.
Until then it keeps a cold-start fallback profile so the sensor always
publishes a full 192-slot contract.
"""

from __future__ import annotations

import asyncio
import json
import math
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..config_flow_helpers import ALL_OPTIMIZER_ENTITY_KEYS, HOUSEHOLD_BINARY_ENTITY_KEYS
from ..const import (
    CONF_CENTRAL_HEATING_POWER_ENTITY,
    CONF_CENTRAL_HEATING_POWER_W,
    CONF_CENTRAL_HEATING_ACTIVE_ENTITY,
    CONF_DEHUMIDIFIER_POWER_ENTITY,
    CONF_DEHUMIDIFIER_POWER_W,
    CONF_POOL_PUMP_POWER_ENTITY,
    CONF_POOL_PUMP_POWER_W,
    CONF_POWER_METER_CONSUMPTION,
    CONF_WATER_HEATER_ACTIVE_ENTITY,
    CONF_WATER_HEATER_POWER_ENTITY,
    CONF_WATER_HEATER_POWER_W,
)

_LOGGER = logging.getLogger(__name__)

WINDOW_START_HOUR = 1
WINDOW_END_HOUR = 4
STORE_VERSION = 1
STORE_MODE = "static"
DB_SCHEMA_VERSION = 1
SLOT_MINUTES = 15
FORECAST_HOURS = 48
FORECAST_SLOT_COUNT = int((FORECAST_HOURS * 60) / SLOT_MINUTES)
FORECAST_COLD_START_MIN_FINALIZED_SLOTS = 24
FORECAST_MIN_HISTORY_DAYS = 7
FORECAST_TRAINING_WINDOW_DAYS = 56
FORECAST_HISTORY_WINDOW_DAYS = 180
FORECAST_TREND_WINDOW_SLOTS = 8
FORECAST_RECENCY_HALF_LIFE_DAYS = 21.0
RESIDUAL_ALPHA = 0.35
RESIDUAL_ACTIVATION_THRESHOLD_KW = 0.25
RESIDUAL_MAX_CORRECTION_KW = 1.5
RESIDUAL_APPLY_HORIZON_SLOTS = 8
RESIDUAL_DECAY_FACTOR = 0.82
RESIDUAL_MIN_CONSECUTIVE_SLOTS = 2
RESIDUAL_MAX_STALE_MINUTES = 30
STATIC_LOAD_FORECAST_W = 600.0
STATIC_LOAD_FORECAST_KW = STATIC_LOAD_FORECAST_W / 1000.0
STATIC_REASON = (
    "Household forecast is in cold-start mode; using a fixed 600 W profile."
)
MAX_INTERVAL_FOR_SPIKES_SEC = 120
MAX_INTERVAL_FOR_TRAINING_SEC = 300
HARD_GAP_SEC = 1800
RAW_REQUIRED_POLL_INTERVAL = timedelta(seconds=60)
RAW_OPTIONAL_POLL_INTERVAL = timedelta(seconds=120)
RAW_HEARTBEAT_SECONDS = float(MAX_INTERVAL_FOR_SPIKES_SEC)
RAW_SAMPLE_RETENTION_DAYS = 21
SLOT_ROW_RETENTION_DAYS = 180
FORECAST_RUN_RETENTION_DAYS = 14
_NUMERIC_STATE_PATTERN = re.compile(r"[-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)")
_ACTIVE_EVENT_STATES = {"on", "true", "active", "heating", "home"}
_INACTIVE_EVENT_STATES = {"off", "false", "inactive", "idle", "standby"}
_POWER_UNITS = {"w", "kw", "mw"}
_ENERGY_UNITS = {"wh", "kwh", "mwh"}


@dataclass(slots=True)
class _CaptureTarget:
    """Describe one configured raw-capture target."""

    key: str
    entity_id: str
    is_event: bool
    poll_interval: timedelta


@dataclass(slots=True)
class _LatestNumericSample:
    """Track the most recent numeric sample for a sensor key."""

    ts_utc: datetime
    value: float
    unit: str | None


@dataclass(slots=True)
class _LatestEventSample:
    """Track the most recent event sample for a binary key."""

    ts_utc: datetime
    state_num: float
    state_text: str


@dataclass(slots=True)
class _RawMeterSample:
    """Track one raw meter sample loaded from SQLite."""

    ts_utc: datetime
    value: float
    unit: str | None
    quality: str


@dataclass(slots=True)
class _RawEventSample:
    """Track one raw event sample loaded from SQLite."""

    ts_utc: datetime
    state_num: float
    state_text: str
    quality: str


@dataclass(slots=True)
class _IntervalEnergyRow:
    """Track one interval-energy row derived from consecutive meter samples."""

    ts_from_utc: datetime
    ts_to_utc: datetime
    delta_kwh: float
    interval_sec: int
    interval_kw: float
    quality: str


@dataclass(slots=True)
class _SlotAccumulator:
    """Accumulate interval energy into one 15-minute slot."""

    slot_start_utc: datetime
    slot_energy_kwh: float = 0.0
    subtracted_energy_kwh: float = 0.0
    observed_seconds: float = 0.0
    sample_count: int = 0
    sparse_interval_count: int = 0


@dataclass(slots=True)
class _WeightedStats:
    """Track a weighted mean for the seasonal profile."""

    weighted_sum: float = 0.0
    weight_sum: float = 0.0
    count: int = 0

    def add(self, value: float, weight: float) -> None:
        """Accumulate one weighted sample."""
        if weight <= 0.0 or not math.isfinite(value):
            return
        self.weighted_sum += value * weight
        self.weight_sum += weight
        self.count += 1

    def mean(self) -> float | None:
        """Return the weighted mean when at least one sample exists."""
        if self.weight_sum <= 0.0:
            return None
        return self.weighted_sum / self.weight_sum


@dataclass(slots=True)
class _HistoricalSlotRow:
    """Track one persisted slot row used to fit the seasonal profile."""

    slot_start_utc: datetime
    load_kw: float
    quality_score: float
    sample_count: int
    slot_index: int
    day_type: str
    is_holiday: bool


@dataclass(slots=True)
class _ForecastSummary:
    """Track the visible learning summary for the sensor attributes."""

    learning_nights: int = 0
    data_since: str | None = None
    last_sample_date: str | None = None
    last_sample_kw: float | None = None
    reason: str = STATIC_REASON


class HouseholdForecastCoordinator:
    """Expose a learned household load forecast profile and keep housekeeping."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        self.hass = hass
        self.entry = entry

        self._listeners: list[Callable[[], None]] = []
        self._capture_listeners: list[Callable[[], None]] = []
        self._update_callbacks: list[Callable[[], None]] = []
        self._load_forecast_kw = STATIC_LOAD_FORECAST_KW
        self._status_message: str = STATIC_REASON
        self._last_forecast_generation = dt_util.now().strftime("%Y-%m-%dT%H:%M")
        self._forecast_slots: list[dict[str, object]] = []
        self._forecast_summary = _ForecastSummary()
        self._store = Store(
            hass,
            STORE_VERSION,
            f"energyadvisor_household_forecast_{entry.entry_id}",
        )

        self._db: sqlite3.Connection | None = None
        self._db_write_lock = asyncio.Lock()
        self._capture_targets: dict[str, _CaptureTarget] = {}
        self._latest_numeric_samples: dict[str, _LatestNumericSample] = {}
        self._latest_event_samples: dict[str, _LatestEventSample] = {}
        self._last_polled_utc_by_key: dict[str, datetime] = {}
        self._last_raw_prune_utc: datetime | None = None
        self._last_finalized_slot_utc: datetime | None = None
        self._last_generation_utc: datetime | None = None

    # -- Config helpers --------------------------------------------------

    @property
    def _meter_entity(self) -> str:
        return self.entry.options.get(CONF_POWER_METER_CONSUMPTION, "")

    @property
    def _water_heater_entity(self) -> str:
        return self.entry.options.get(CONF_WATER_HEATER_ACTIVE_ENTITY, "")

    @property
    def _central_heating_entity(self) -> str:
        return self.entry.options.get(CONF_CENTRAL_HEATING_ACTIVE_ENTITY, "")

    def _option_float(self, key: str) -> float | None:
        """Return a numeric config option when it is present and valid."""
        value = self.entry.options.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def _water_heater_power_entity(self) -> str:
        return self.entry.options.get(CONF_WATER_HEATER_POWER_ENTITY, "")

    @property
    def _water_heater_power_w(self) -> float | None:
        return self._option_float(CONF_WATER_HEATER_POWER_W)

    @property
    def _central_heating_power_entity(self) -> str:
        return self.entry.options.get(CONF_CENTRAL_HEATING_POWER_ENTITY, "")

    @property
    def _central_heating_power_w(self) -> float | None:
        return self._option_float(CONF_CENTRAL_HEATING_POWER_W)

    @property
    def _pool_pump_power_entity(self) -> str:
        return self.entry.options.get(CONF_POOL_PUMP_POWER_ENTITY, "")

    @property
    def _pool_pump_power_w(self) -> float | None:
        return self._option_float(CONF_POOL_PUMP_POWER_W)

    @property
    def _dehumidifier_power_entity(self) -> str:
        return self.entry.options.get(CONF_DEHUMIDIFIER_POWER_ENTITY, "")

    @property
    def _dehumidifier_power_w(self) -> float | None:
        return self._option_float(CONF_DEHUMIDIFIER_POWER_W)

    # -- Public forecast values ------------------------------------------

    @property
    def load_forecast_kw(self) -> float:
        """Return the current household load forecast."""
        if self._forecast_slots:
            first_slot = self._forecast_slots[0]
            try:
                return round(float(first_slot["load"]), 3)
            except (KeyError, TypeError, ValueError):
                pass
        return self._load_forecast_kw

    @property
    def base_load_kw(self) -> float:
        """Backward-compatible alias for load forecast in kW."""
        return self.load_forecast_kw

    @property
    def household_load_forecast_w(self) -> float:
        """Return the current household load forecast in watts."""
        return self._load_forecast_kw * 1000.0

    @property
    def household_base_load_w(self) -> float:
        """Backward-compatible alias for load forecast in watts."""
        return self.household_load_forecast_w

    @property
    def forecast_slots(self) -> list[dict[str, object]]:
        """Return 15-minute load slots for the 48-hour horizon."""
        if self._forecast_slots:
            return [dict(slot) for slot in self._forecast_slots]
        return self._build_constant_forecast_slots(self._load_forecast_kw)

    @property
    def learning_nights(self) -> int:
        """Return the number of retained learning days."""
        return self._forecast_summary.learning_nights

    @property
    def data_since(self) -> str | None:
        """Return the oldest retained learning date."""
        return self._forecast_summary.data_since

    @property
    def last_sample_date(self) -> str | None:
        """Return the most recent learned sample date."""
        return self._forecast_summary.last_sample_date

    @property
    def last_sample_kw(self) -> float | None:
        """Return the most recent learned sample in kW."""
        return self._forecast_summary.last_sample_kw

    @property
    def reason(self) -> str:
        """Return a human-readable status message."""
        if self._status_message != STATIC_REASON:
            return self._status_message
        return self._forecast_summary.reason or self._status_message

    @property
    def last_forecast_generation(self) -> str:
        """Return the timestamp of the last forecast shell generation."""
        return self._last_forecast_generation

    # -- Lifecycle -------------------------------------------------------

    async def async_setup(self) -> None:
        """Register listeners and load housekeeping state."""
        await self._async_load_state()
        if not self._meter_entity:
            _LOGGER.warning(
                "Household forecast cannot start because the household meter is missing"
            )
            self._set_status(
                "Household forecast is waiting for the required household meter."
            )
            return

        self._capture_targets = self._build_capture_targets()
        await self._async_initialize_capture_db()

        quiet_sensor_entities = [
            entity_id
            for entity_id in (
                self._water_heater_entity,
                self._central_heating_entity,
            )
            if entity_id
        ]
        if quiet_sensor_entities:
            self._listeners.append(
                async_track_state_change_event(
                    self.hass,
                    quiet_sensor_entities,
                    self._handle_quiet_sensor_change,
                )
            )
        self._listeners.append(
            async_track_time_change(
                self.hass,
                self._handle_window_start,
                hour=WINDOW_START_HOUR,
                minute=0,
                second=0,
            )
        )
        self._listeners.append(
            async_track_time_change(
                self.hass,
                self._handle_window_finish,
                hour=WINDOW_END_HOUR,
                minute=0,
                second=0,
            )
        )
        self._listeners.append(
            async_track_time_change(
                self.hass,
                self._handle_forecast_refresh,
                minute=[0, 15, 30, 45],
                second=0,
            )
        )

        self._capture_listeners.append(
            async_track_state_change_event(
                self.hass,
                list(self._capture_targets),
                self._handle_raw_state_change,
            )
        )
        self._capture_listeners.append(
            async_track_time_interval(
                self.hass,
                self._handle_raw_poll,
                RAW_REQUIRED_POLL_INTERVAL,
            )
        )
        _LOGGER.info(
            "Household forecast refresh cadence set to quarter-hour for validation; "
            "the 15-minute slot grid remains day-anchored."
        )
        self._set_status(STATIC_REASON)
        rebuild_now = dt_util.utcnow()
        if await self._async_finalize_history(rebuild_now):
            self._last_forecast_generation = dt_util.as_local(rebuild_now).strftime(
                "%Y-%m-%dT%H:%M"
            )

    async def async_shutdown(self) -> None:
        """Remove listeners and stop scheduling updates."""
        for remove in self._capture_listeners:
            remove()
        self._capture_listeners.clear()
        for remove in self._listeners:
            remove()
        self._listeners.clear()
        if self._db is not None:
            await self.hass.async_add_executor_job(self._close_db_sync)

    def register_update_callback(self, cb: Callable[[], None]) -> None:
        """Register a callback for state changes."""
        self._update_callbacks.append(cb)

    def unregister_update_callback(self, cb: Callable[[], None]) -> None:
        """Remove a previously registered callback."""
        try:
            self._update_callbacks.remove(cb)
        except ValueError:
            pass

    # -- Internal helpers ------------------------------------------------

    def _notify_update(self) -> None:
        """Notify listeners that public values changed."""
        for cb in tuple(self._update_callbacks):
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error notifying household forecast listeners")

    def _set_status(self, message: str) -> None:
        """Update the status message and notify listeners when changed."""
        if message == self._status_message:
            return
        self._status_message = message
        self._notify_update()

    def _serialize_state(self) -> dict[str, Any]:
        """Serialize static housekeeping state."""
        return {
            "mode": STORE_MODE,
            "load_forecast_kw": self.load_forecast_kw,
        }

    def _restore_state(self, data: Mapping[str, Any]) -> bool:
        """Restore static housekeeping state from storage.

        Returns True when the payload should be normalized and persisted.
        """
        legacy_payload = "samples" in data or "window" in data
        if legacy_payload or "base_load_kw" in data:
            self._load_forecast_kw = STATIC_LOAD_FORECAST_KW
            return True

        restored_load_forecast = data.get("load_forecast_kw")
        if restored_load_forecast is None:
            self._load_forecast_kw = STATIC_LOAD_FORECAST_KW
            return True

        try:
            self._load_forecast_kw = float(restored_load_forecast)
        except (TypeError, ValueError):
            self._load_forecast_kw = STATIC_LOAD_FORECAST_KW
            return True

        return data.get("mode") != STORE_MODE

    async def _async_load_state(self) -> None:
        """Load persisted housekeeping state from storage."""
        data = await self._store.async_load()
        if not data:
            return
        if not isinstance(data, Mapping):
            _LOGGER.warning("Household forecast storage contained unexpected data")
            return
        changed = self._restore_state(data)
        if changed:
            await self._async_save_state()

    async def _async_save_state(self) -> None:
        """Persist static housekeeping state."""
        await self._store.async_save(self._serialize_state())

    def _schedule_state_save(self) -> None:
        """Schedule persistence after a state change."""
        if self.hass is None:
            return
        save_coro = self._async_save_state()
        save_task = self.hass.async_create_task(save_coro)
        if save_task is None:
            # Test doubles may not actually schedule the coroutine.
            # Close it so Python does not warn about an un-awaited coroutine.
            save_coro.close()
            return

    @callback
    def _handle_window_start(self, _now=None) -> None:
        """Retained for structure while learning is disabled."""
        self._set_status(STATIC_REASON)
        self._schedule_state_save()

    @callback
    def _handle_quiet_sensor_change(self, _event) -> None:
        """Retained for structure while learning is disabled."""
        self._set_status(STATIC_REASON)

    @callback
    def _handle_forecast_refresh(self, _now=None) -> None:
        """Refresh the forecast shell on the validation heartbeat."""
        refresh_now = dt_util.now()
        self._last_forecast_generation = refresh_now.strftime("%Y-%m-%dT%H:%M")
        _LOGGER.info(
            "Household forecast refresh tick at %s: republishing the day-anchored shell",
            self._last_forecast_generation,
        )
        self._notify_update()
        self._schedule_async_task(self._async_refresh_forecast(refresh_now))

    @callback
    def _handle_window_finish(self, _now=None) -> None:
        """Retained for structure while learning is disabled."""
        self._set_status(STATIC_REASON)
        self._schedule_state_save()

    # -- SQLite persistence ----------------------------------------------

    def _db_path(self) -> str:
        storage_dir = os.path.join(self.hass.config.config_dir, ".storage")
        os.makedirs(storage_dir, exist_ok=True)
        return os.path.join(storage_dir, f"energyadvisor_household_forecast_{self.entry.entry_id}.db")

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_samples (
                ts_utc TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT,
                quality TEXT NOT NULL,
                PRIMARY KEY (ts_utc, sensor_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_samples_sensor_ts "
            "ON raw_samples(sensor_key, ts_utc DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interval_energy (
                ts_from_utc TEXT NOT NULL,
                ts_to_utc TEXT NOT NULL,
                sensor_key TEXT NOT NULL,
                delta_kwh REAL NOT NULL,
                interval_sec INTEGER NOT NULL,
                interval_kw REAL NOT NULL,
                quality TEXT NOT NULL,
                PRIMARY KEY (ts_from_utc, sensor_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_interval_energy_sensor_to "
            "ON interval_energy(sensor_key, ts_to_utc DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_events (
                ts_utc TEXT NOT NULL,
                event_key TEXT NOT NULL,
                state_num REAL,
                state_text TEXT,
                quality TEXT NOT NULL,
                PRIMARY KEY (ts_utc, event_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_events_key_ts "
            "ON raw_events(event_key, ts_utc DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS slot_rows (
                slot_start_utc TEXT NOT NULL PRIMARY KEY,
                slot_energy_kwh REAL NOT NULL,
                load_kw REAL NOT NULL,
                sample_count INTEGER NOT NULL,
                quality_score REAL NOT NULL,
                features_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forecast_runs (
                generated_at_utc TEXT NOT NULL,
                slot_start_utc TEXT NOT NULL,
                load_kw REAL NOT NULL,
                PRIMARY KEY (generated_at_utc, slot_start_utc)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_forecast_runs_generated "
            "ON forecast_runs(generated_at_utc DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT NOT NULL PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        version_row = conn.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0] if version_row else 0)
        if version < DB_SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
        conn.commit()
        return conn

    def _ensure_db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = self._open_db()
        return self._db

    def _close_db_sync(self) -> None:
        """Checkpoint WAL then close the database connection."""
        if self._db is not None:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:  # noqa: BLE001
                pass
            self._db.close()
            self._db = None

    def _parse_utc_timestamp(self, timestamp: str) -> datetime:
        """Parse a stored UTC timestamp string."""
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _format_utc_timestamp(self, timestamp: datetime) -> str:
        """Format a UTC timestamp for storage."""
        utc_timestamp = timestamp.astimezone(timezone.utc)
        return utc_timestamp.isoformat(timespec="seconds")

    def _normalize_utc_timestamp(self, timestamp: datetime | None) -> datetime:
        """Return a timezone-aware UTC timestamp."""
        if timestamp is None:
            return dt_util.utcnow()
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)

    def _state_timestamp_utc(
        self, state: State, fallback: datetime | None = None
    ) -> datetime:
        """Return a UTC timestamp for a Home Assistant state."""
        timestamp = getattr(state, "last_updated", None) or getattr(
            state, "last_changed", None
        )
        if timestamp is None:
            timestamp = fallback or dt_util.utcnow()
        return self._normalize_utc_timestamp(timestamp)

    def _extract_unit(self, state: State) -> str | None:
        """Return the reported unit of measurement, if any."""
        attributes = getattr(state, "attributes", {})
        if not isinstance(attributes, Mapping):
            return None
        unit = attributes.get("unit_of_measurement")
        if unit is None:
            unit = attributes.get("unit")
        if unit is None:
            return None
        unit_text = str(unit).strip()
        return unit_text or None

    def _coerce_numeric_state(self, state_value: object) -> float | None:
        """Parse a numeric Home Assistant state string."""
        if state_value is None:
            return None
        text = str(state_value).strip()
        if not text or text in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return None
        match = _NUMERIC_STATE_PATTERN.search(text.replace("\xa0", " "))
        if match is None:
            return None
        try:
            return float(match.group(0).replace(",", "."))
        except ValueError:
            return None

    def _coerce_event_state_num(self, state_text: str) -> float | None:
        """Map a binary/on-off style state string to a numeric flag."""
        normalized = state_text.strip().lower()
        if normalized in _ACTIVE_EVENT_STATES:
            return 1.0
        if normalized in _INACTIVE_EVENT_STATES:
            return 0.0
        return None

    def _build_capture_targets(self) -> dict[str, _CaptureTarget]:
        """Collect all configured entities that should be raw-captured."""
        targets: dict[str, _CaptureTarget] = {}
        for key in ALL_OPTIMIZER_ENTITY_KEYS:
            entity_id = self.entry.options.get(key)
            if not isinstance(entity_id, str) or not entity_id:
                continue
            poll_interval = (
                RAW_REQUIRED_POLL_INTERVAL
                if key == CONF_POWER_METER_CONSUMPTION
                else RAW_OPTIONAL_POLL_INTERVAL
            )
            targets[entity_id] = _CaptureTarget(
                key=key,
                entity_id=entity_id,
                is_event=key in HOUSEHOLD_BINARY_ENTITY_KEYS,
                poll_interval=poll_interval,
            )
        return targets

    def _upsert_raw_sample_sync(
        self,
        sensor_key: str,
        ts_utc: datetime,
        value: float,
        unit: str | None,
        quality: str,
    ) -> None:
        """Store one raw numeric sample."""
        db = self._ensure_db()
        db.execute(
            "INSERT OR REPLACE INTO raw_samples "
            "(ts_utc, sensor_key, value, unit, quality) VALUES (?, ?, ?, ?, ?)",
            (
                self._format_utc_timestamp(ts_utc),
                sensor_key,
                value,
                unit,
                quality,
            ),
        )
        db.commit()

    def _upsert_raw_event_sync(
        self,
        event_key: str,
        ts_utc: datetime,
        state_num: float,
        state_text: str,
        quality: str,
    ) -> None:
        """Store one raw event sample."""
        db = self._ensure_db()
        db.execute(
            "INSERT OR REPLACE INTO raw_events "
            "(ts_utc, event_key, state_num, state_text, quality) VALUES (?, ?, ?, ?, ?)",
            (
                self._format_utc_timestamp(ts_utc),
                event_key,
                state_num,
                state_text,
                quality,
            ),
        )
        db.commit()

    def _purge_old_data_sync(self, now_utc: datetime) -> None:
        """Prune aged rows from all persistence tables."""
        db = self._ensure_db()
        raw_cutoff = self._format_utc_timestamp(
            now_utc - timedelta(days=RAW_SAMPLE_RETENTION_DAYS)
        )
        slot_cutoff = self._format_utc_timestamp(
            now_utc - timedelta(days=SLOT_ROW_RETENTION_DAYS)
        )
        forecast_cutoff = self._format_utc_timestamp(
            now_utc - timedelta(days=FORECAST_RUN_RETENTION_DAYS)
        )
        db.execute("DELETE FROM raw_samples WHERE ts_utc < ?", (raw_cutoff,))
        db.execute("DELETE FROM interval_energy WHERE ts_to_utc < ?", (raw_cutoff,))
        db.execute("DELETE FROM raw_events WHERE ts_utc < ?", (raw_cutoff,))
        db.execute("DELETE FROM slot_rows WHERE slot_start_utc < ?", (slot_cutoff,))
        db.execute(
            "DELETE FROM forecast_runs WHERE generated_at_utc < ?",
            (forecast_cutoff,),
        )
        db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("last_raw_prune_utc", self._format_utc_timestamp(now_utc)),
        )
        db.commit()

    def _load_capture_cache_sync(self) -> None:
        """Load the most recent persisted raw rows into memory."""
        db = self._ensure_db()
        self._latest_numeric_samples.clear()
        self._latest_event_samples.clear()
        self._last_polled_utc_by_key.clear()
        self._forecast_slots = []
        self._forecast_summary = _ForecastSummary()

        for target in self._capture_targets.values():
            if target.is_event:
                row = db.execute(
                    "SELECT ts_utc, state_num, state_text, quality FROM raw_events "
                    "WHERE event_key = ? ORDER BY ts_utc DESC LIMIT 1",
                    (target.key,),
                ).fetchone()
                if row is None:
                    continue
                self._latest_event_samples[target.key] = _LatestEventSample(
                    ts_utc=self._parse_utc_timestamp(str(row[0])),
                    state_num=float(row[1]) if row[1] is not None else 0.0,
                    state_text=str(row[2] or ""),
                )
                continue

            row = db.execute(
                "SELECT ts_utc, value, unit, quality FROM raw_samples "
                "WHERE sensor_key = ? ORDER BY ts_utc DESC LIMIT 1",
                (target.key,),
            ).fetchone()
            if row is None:
                continue
            self._latest_numeric_samples[target.key] = _LatestNumericSample(
                ts_utc=self._parse_utc_timestamp(str(row[0])),
                value=float(row[1]),
                unit=str(row[2]).strip() if row[2] is not None else None,
            )
        generation_row = db.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("last_generation_utc",),
        ).fetchone()
        if generation_row and generation_row[0]:
            self._last_generation_utc = self._parse_utc_timestamp(str(generation_row[0]))
            self._last_forecast_generation = dt_util.as_local(
                self._last_generation_utc
            ).strftime("%Y-%m-%dT%H:%M")

        finalized_row = db.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("last_finalized_slot_utc",),
        ).fetchone()
        if finalized_row and finalized_row[0]:
            self._last_finalized_slot_utc = self._parse_utc_timestamp(
                str(finalized_row[0])
            )

        prune_row = db.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("last_raw_prune_utc",),
        ).fetchone()
        if prune_row and prune_row[0]:
            self._last_raw_prune_utc = self._parse_utc_timestamp(str(prune_row[0]))

        latest_forecast_row = db.execute(
            "SELECT generated_at_utc FROM forecast_runs "
            "ORDER BY generated_at_utc DESC LIMIT 1"
        ).fetchone()
        if latest_forecast_row and latest_forecast_row[0]:
            latest_generation_utc = self._parse_utc_timestamp(
                str(latest_forecast_row[0])
            )
            forecast_rows = db.execute(
                "SELECT slot_start_utc, load_kw FROM forecast_runs "
                "WHERE generated_at_utc = ? ORDER BY slot_start_utc ASC",
                (self._format_utc_timestamp(latest_generation_utc),),
            ).fetchall()
            self._forecast_slots = [
                {
                    "from": dt_util.as_local(
                        self._parse_utc_timestamp(str(row[0]))
                    ).strftime("%Y-%m-%dT%H:%M"),
                    "load": round(float(row[1]), 3),
                }
                for row in forecast_rows
            ]
            if self._forecast_slots:
                try:
                    self._load_forecast_kw = float(self._forecast_slots[0]["load"])
                except (TypeError, ValueError, KeyError):
                    self._load_forecast_kw = STATIC_LOAD_FORECAST_KW

        history_rows = self._load_historical_slot_rows_sync()
        if history_rows:
            self._forecast_summary = self._build_forecast_summary(history_rows)

    async def _async_initialize_capture_db(self) -> None:
        """Open the SQLite history file and prime in-memory caches."""
        self._db = await self.hass.async_add_executor_job(self._open_db)
        await self.hass.async_add_executor_job(self._load_capture_cache_sync)

    async def _async_prune_raw_history(self, now_utc: datetime | None = None) -> None:
        """Prune old raw rows when the daily retention window has elapsed."""
        prune_now = self._normalize_utc_timestamp(now_utc)
        if (
            self._last_raw_prune_utc is not None
            and (prune_now - self._last_raw_prune_utc).total_seconds() < 86400
        ):
            return
        async with self._db_write_lock:
            await self.hass.async_add_executor_job(self._purge_old_data_sync, prune_now)
        self._last_raw_prune_utc = prune_now

    async def _async_store_numeric_sample(
        self,
        target: _CaptureTarget,
        state: State,
        *,
        source: str,
        captured_at: datetime | None = None,
    ) -> None:
        """Persist a numeric sample when it is new or due for a heartbeat."""
        value = self._coerce_numeric_state(getattr(state, "state", None))
        if value is None:
            return
        unit = self._extract_unit(state)
        ts_utc = self._normalize_utc_timestamp(
            captured_at or self._state_timestamp_utc(state)
        )

        last_sample = self._latest_numeric_samples.get(target.key)
        quality = "ok"
        if source == "poll" and last_sample is not None:
            same_value = last_sample.value == value and last_sample.unit == unit
            age_seconds = (ts_utc - last_sample.ts_utc).total_seconds()
            if same_value and age_seconds < RAW_HEARTBEAT_SECONDS:
                return
            if same_value:
                quality = "heartbeat"

        async with self._db_write_lock:
            await self.hass.async_add_executor_job(
                self._upsert_raw_sample_sync,
                target.key,
                ts_utc,
                value,
                unit,
                quality,
            )
        self._latest_numeric_samples[target.key] = _LatestNumericSample(
            ts_utc=ts_utc,
            value=value,
            unit=unit,
        )

    async def _async_store_event_sample(
        self,
        target: _CaptureTarget,
        state: State,
        *,
        source: str,
        captured_at: datetime | None = None,
    ) -> None:
        """Persist a binary/event sample when it is new or due for a heartbeat."""
        state_text = str(getattr(state, "state", "")).strip()
        if not state_text or state_text in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return
        state_num = self._coerce_event_state_num(state_text)
        if state_num is None:
            return
        ts_utc = self._normalize_utc_timestamp(
            captured_at or self._state_timestamp_utc(state)
        )

        last_sample = self._latest_event_samples.get(target.key)
        quality = "ok"
        if source == "poll" and last_sample is not None:
            same_value = (
                last_sample.state_num == state_num
                and last_sample.state_text == state_text
            )
            age_seconds = (ts_utc - last_sample.ts_utc).total_seconds()
            if same_value and age_seconds < RAW_HEARTBEAT_SECONDS:
                return
            if same_value:
                quality = "heartbeat"

        async with self._db_write_lock:
            await self.hass.async_add_executor_job(
                self._upsert_raw_event_sync,
                target.key,
                ts_utc,
                state_num,
                state_text,
                quality,
            )
        self._latest_event_samples[target.key] = _LatestEventSample(
            ts_utc=ts_utc,
            state_num=state_num,
            state_text=state_text,
        )

    async def _async_capture_target(
        self,
        target: _CaptureTarget,
        state: State,
        *,
        source: str,
        captured_at: datetime | None = None,
    ) -> None:
        """Persist a state snapshot for one configured raw-capture target."""
        if target.is_event:
            await self._async_store_event_sample(
                target,
                state,
                source=source,
                captured_at=captured_at,
            )
            return
        await self._async_store_numeric_sample(
            target,
            state,
            source=source,
            captured_at=captured_at,
        )

    def _schedule_async_task(self, coro: Any) -> None:
        """Schedule a coroutine and close it if test doubles do not run tasks."""
        task = self.hass.async_create_task(coro)
        if task is None and hasattr(coro, "close"):
            coro.close()

    @callback
    def _handle_raw_state_change(self, event) -> None:
        """Persist state changes for the configured raw-capture entities."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        entity_id = getattr(new_state, "entity_id", None)
        if not entity_id:
            return
        target = self._capture_targets.get(entity_id)
        if target is None:
            return
        self._schedule_async_task(
            self._async_capture_target(target, new_state, source="event")
        )

    @callback
    def _handle_raw_poll(self, now: datetime | None = None) -> None:
        """Poll configured raw-capture targets to backfill heartbeat samples."""
        self._schedule_async_task(self._async_poll_targets(now))

    async def _async_poll_targets(self, now: datetime | None = None) -> None:
        """Poll tracked entities when their configured interval has elapsed."""
        poll_now = self._normalize_utc_timestamp(now)
        for target in self._capture_targets.values():
            last_polled = self._last_polled_utc_by_key.get(target.key)
            if last_polled is not None:
                elapsed = (poll_now - last_polled).total_seconds()
                if elapsed < target.poll_interval.total_seconds():
                    continue
            self._last_polled_utc_by_key[target.key] = poll_now

            state = self.hass.states.get(target.entity_id)
            if state is None or getattr(state, "state", None) in {
                STATE_UNKNOWN,
                STATE_UNAVAILABLE,
            }:
                continue
            await self._async_capture_target(
                target,
                state,
                source="poll",
                captured_at=poll_now,
            )

        await self._async_prune_raw_history(poll_now)

    # -- Energy aggregation ----------------------------------------------

    def _unit_family(self, unit: str | None) -> str | None:
        """Classify a unit as power or energy."""
        if unit is None:
            return None
        normalized = unit.strip().lower()
        if normalized in _POWER_UNITS:
            return "power"
        if normalized in _ENERGY_UNITS:
            return "energy"
        return None

    def _unit_scale(self, unit: str | None) -> float | None:
        """Return the scale needed to convert a unit to kW or kWh."""
        if unit is None:
            return None
        normalized = unit.strip().lower()
        return {
            "w": 0.001,
            "kw": 1.0,
            "mw": 1000.0,
            "wh": 0.001,
            "kwh": 1.0,
            "mwh": 1000.0,
        }.get(normalized)

    def _value_to_kw(self, value: float, unit: str | None) -> float | None:
        """Convert a power value to kW."""
        if self._unit_family(unit) != "power":
            return None
        scale = self._unit_scale(unit)
        if scale is None:
            return None
        return value * scale

    def _value_to_kwh(self, value: float, unit: str | None) -> float | None:
        """Convert an energy counter value to kWh."""
        if self._unit_family(unit) != "energy":
            return None
        scale = self._unit_scale(unit)
        if scale is None:
            return None
        return value * scale

    def _floor_to_slot_start_utc(self, timestamp: datetime) -> datetime:
        """Floor a timestamp to the start of its 15-minute slot."""
        normalized = self._normalize_utc_timestamp(timestamp)
        slot_minute = (normalized.minute // SLOT_MINUTES) * SLOT_MINUTES
        return normalized.replace(minute=slot_minute, second=0, microsecond=0)

    def _slot_day_type(self, slot_start_utc: datetime) -> str:
        """Map a slot start to the default day-type bucket."""
        local_slot_start = dt_util.as_local(slot_start_utc)
        weekday = local_slot_start.weekday()
        if weekday < 5:
            return "workday"
        if weekday == 5:
            return "saturday"
        return "sunday"

    def _slot_index(self, slot_start_utc: datetime) -> int:
        """Return the local 15-minute slot index within the day."""
        local_slot_start = dt_util.as_local(slot_start_utc)
        return (local_slot_start.hour * 60 + local_slot_start.minute) // SLOT_MINUTES

    def _event_flag_value(self, key: str) -> int:
        """Return 1 when the latest cached event sample is active."""
        sample = self._latest_event_samples.get(key)
        if sample is None:
            return 0
        return 1 if sample.state_num > 0.0 else 0

    def _build_interval_energy_row(
        self,
        previous: _RawMeterSample,
        current: _RawMeterSample,
    ) -> _IntervalEnergyRow | None:
        """Convert two meter samples into one interval-energy row."""
        interval_sec = int((current.ts_utc - previous.ts_utc).total_seconds())
        if interval_sec <= 0:
            return None

        previous_family = self._unit_family(previous.unit)
        current_family = self._unit_family(current.unit)
        if previous_family is None or current_family is None:
            return None
        if previous_family != current_family:
            return None

        quality = "ok"
        if interval_sec > MAX_INTERVAL_FOR_TRAINING_SEC:
            quality = "sparse_gap"
        elif previous.quality == "heartbeat" or current.quality == "heartbeat":
            quality = "heartbeat"

        if previous_family == "power":
            previous_kw = self._value_to_kw(previous.value, previous.unit)
            current_kw = self._value_to_kw(current.value, current.unit)
            if previous_kw is None or current_kw is None:
                return None
            delta_kwh = ((previous_kw + current_kw) / 2.0) * (
                interval_sec / 3600.0
            )
        else:
            previous_kwh = self._value_to_kwh(previous.value, previous.unit)
            current_kwh = self._value_to_kwh(current.value, current.unit)
            if previous_kwh is None or current_kwh is None:
                return None
            delta_kwh = current_kwh - previous_kwh
            if delta_kwh < 0:
                return None

        interval_hours = interval_sec / 3600.0
        interval_kw = delta_kwh / interval_hours if interval_hours > 0 else 0.0
        if not math.isfinite(delta_kwh) or not math.isfinite(interval_kw):
            return None
        if delta_kwh < 0:
            return None

        return _IntervalEnergyRow(
            ts_from_utc=previous.ts_utc,
            ts_to_utc=current.ts_utc,
            delta_kwh=delta_kwh,
            interval_sec=interval_sec,
            interval_kw=interval_kw,
            quality=quality,
        )

    def _build_slot_features_json(
        self,
        slot_start_utc: datetime,
        accumulator: _SlotAccumulator,
    ) -> str:
        """Serialize the slot feature contract for persistence."""
        slot_duration_seconds = SLOT_MINUTES * 60
        features = {
            "slot_index": self._slot_index(slot_start_utc),
            "day_type": self._slot_day_type(slot_start_utc),
            "is_holiday": False,
            "gross_load_kwh": round(accumulator.slot_energy_kwh, 6),
            "known_load_kwh": round(accumulator.subtracted_energy_kwh, 6),
            "known_load_kw": round(
                accumulator.subtracted_energy_kwh / (SLOT_MINUTES / 60.0), 3
            ),
            "interval_count": accumulator.sample_count,
            "sparse_interval_count": accumulator.sparse_interval_count,
            "required_missing": (
                accumulator.sample_count == 0
                or accumulator.observed_seconds + 1e-6 < slot_duration_seconds
            ),
            "outdoor_temp_c": None,
            "event_flags": {
                "water_heater_active": self._event_flag_value(
                    CONF_WATER_HEATER_ACTIVE_ENTITY
                ),
                "central_heating_active": self._event_flag_value(
                    CONF_CENTRAL_HEATING_ACTIVE_ENTITY
                ),
            },
        }
        return json.dumps(features, separators=(",", ":"), sort_keys=True)

    def _build_slot_row(
        self,
        slot_start_utc: datetime,
        accumulator: _SlotAccumulator,
    ) -> tuple[str, float, float, int, float, str]:
        """Convert one slot accumulator into a database row."""
        slot_duration_hours = SLOT_MINUTES / 60.0
        gross_slot_energy_kwh = accumulator.slot_energy_kwh
        net_slot_energy_kwh = max(
            0.0, gross_slot_energy_kwh - accumulator.subtracted_energy_kwh
        )
        load_kw = (
            net_slot_energy_kwh / slot_duration_hours if slot_duration_hours > 0 else 0.0
        )
        slot_duration_seconds = SLOT_MINUTES * 60
        coverage_fraction = min(1.0, accumulator.observed_seconds / slot_duration_seconds)
        quality_score = 0.0
        if accumulator.sample_count > 0:
            quality_score = coverage_fraction
            if accumulator.sparse_interval_count:
                sparse_fraction = accumulator.sparse_interval_count / accumulator.sample_count
                quality_score *= max(0.0, 1.0 - (0.5 * sparse_fraction))
        return (
            self._format_utc_timestamp(slot_start_utc),
            net_slot_energy_kwh,
            load_kw,
            accumulator.sample_count,
            quality_score,
            self._build_slot_features_json(slot_start_utc, accumulator),
        )

    def _load_numeric_samples_sync(self, sensor_key: str) -> list[_RawMeterSample]:
        """Load retained numeric samples for one configured sensor."""
        db = self._ensure_db()
        rows = db.execute(
            "SELECT ts_utc, value, unit, quality FROM raw_samples "
            "WHERE sensor_key = ? ORDER BY ts_utc ASC",
            (sensor_key,),
        ).fetchall()
        return [
            _RawMeterSample(
                ts_utc=self._parse_utc_timestamp(str(row[0])),
                value=float(row[1]),
                unit=str(row[2]).strip() if row[2] is not None else None,
                quality=str(row[3] or "ok"),
            )
            for row in rows
        ]

    def _load_meter_samples_sync(self) -> list[_RawMeterSample]:
        """Load all retained raw meter samples ordered by timestamp."""
        return self._load_numeric_samples_sync(CONF_POWER_METER_CONSUMPTION)

    def _load_event_samples_sync(self, event_key: str) -> list[_RawEventSample]:
        """Load retained event samples for one configured binary sensor."""
        db = self._ensure_db()
        rows = db.execute(
            "SELECT ts_utc, state_num, state_text, quality FROM raw_events "
            "WHERE event_key = ? ORDER BY ts_utc ASC",
            (event_key,),
        ).fetchall()
        return [
            _RawEventSample(
                ts_utc=self._parse_utc_timestamp(str(row[0])),
                state_num=float(row[1]) if row[1] is not None else 0.0,
                state_text=str(row[2] or ""),
                quality=str(row[3] or "ok"),
            )
            for row in rows
        ]

    def _build_interval_energy_rows(
        self, samples: list[_RawMeterSample]
    ) -> list[_IntervalEnergyRow]:
        """Derive interval-energy rows from a sequence of meter samples."""
        interval_rows: list[_IntervalEnergyRow] = []
        previous_sample: _RawMeterSample | None = None
        for sample in samples:
            if previous_sample is None:
                previous_sample = sample
                continue
            interval_row = self._build_interval_energy_row(previous_sample, sample)
            previous_sample = sample
            if interval_row is not None:
                interval_rows.append(interval_row)
        return interval_rows

    def _build_active_interval_energy_rows(
        self,
        samples: list[_RawEventSample],
        active_kw: float,
    ) -> list[_IntervalEnergyRow]:
        """Derive interval-energy rows for a binary-active appliance."""
        interval_rows: list[_IntervalEnergyRow] = []
        if active_kw <= 0.0:
            return interval_rows

        previous_sample: _RawEventSample | None = None
        for sample in samples:
            if previous_sample is None:
                previous_sample = sample
                continue

            current_sample = sample
            interval_sec = int(
                (current_sample.ts_utc - previous_sample.ts_utc).total_seconds()
            )
            if interval_sec <= 0:
                previous_sample = current_sample
                continue

            quality = "ok"
            if interval_sec > MAX_INTERVAL_FOR_TRAINING_SEC:
                quality = "sparse_gap"
            elif (
                previous_sample.quality == "heartbeat"
                or current_sample.quality == "heartbeat"
            ):
                quality = "heartbeat"

            if previous_sample.state_num <= 0.0:
                previous_sample = current_sample
                continue

            delta_kwh = active_kw * (interval_sec / 3600.0)
            interval_rows.append(
                _IntervalEnergyRow(
                    ts_from_utc=previous_sample.ts_utc,
                    ts_to_utc=current_sample.ts_utc,
                    delta_kwh=delta_kwh,
                    interval_sec=interval_sec,
                    interval_kw=active_kw,
                    quality=quality,
                )
            )
            previous_sample = current_sample
        return interval_rows

    def _load_appliance_interval_rows_sync(
        self,
        sensor_key: str,
        *,
        active_key: str | None = None,
        fallback_kw: float | None = None,
    ) -> list[_IntervalEnergyRow]:
        """Load interval rows for one subtractive appliance source."""
        if not sensor_key:
            return []

        interval_rows = self._build_interval_energy_rows(
            self._load_numeric_samples_sync(sensor_key)
        )
        if interval_rows or active_key is None or fallback_kw is None:
            return interval_rows
        if fallback_kw <= 0.0:
            return interval_rows

        event_rows = self._load_event_samples_sync(active_key)
        if not event_rows:
            return interval_rows
        return self._build_active_interval_energy_rows(event_rows, fallback_kw)

    def _build_appliance_subtraction_rows_sync(self) -> list[_IntervalEnergyRow]:
        """Collect all known appliance interval rows to subtract from the meter."""
        rows: list[_IntervalEnergyRow] = []
        rows.extend(
            self._load_appliance_interval_rows_sync(
                CONF_WATER_HEATER_POWER_ENTITY,
                active_key=CONF_WATER_HEATER_ACTIVE_ENTITY,
                fallback_kw=self._water_heater_power_w / 1000.0
                if self._water_heater_power_w is not None
                else None,
            )
        )
        rows.extend(
            self._load_appliance_interval_rows_sync(
                CONF_CENTRAL_HEATING_POWER_ENTITY,
                active_key=CONF_CENTRAL_HEATING_ACTIVE_ENTITY,
                fallback_kw=self._central_heating_power_w / 1000.0
                if self._central_heating_power_w is not None
                else None,
            )
        )
        rows.extend(
            self._load_appliance_interval_rows_sync(
                CONF_POOL_PUMP_POWER_ENTITY,
                fallback_kw=self._pool_pump_power_w / 1000.0
                if self._pool_pump_power_w is not None
                else None,
            )
        )
        rows.extend(
            self._load_appliance_interval_rows_sync(
                CONF_DEHUMIDIFIER_POWER_ENTITY,
                fallback_kw=self._dehumidifier_power_w / 1000.0
                if self._dehumidifier_power_w is not None
                else None,
            )
        )
        return rows

    def _build_constant_forecast_slots(
        self,
        load_kw: float,
        now_utc: datetime | None = None,
    ) -> list[dict[str, object]]:
        """Build a fixed 192-slot forecast anchored to the current local day."""
        local_now = dt_util.as_local(self._normalize_utc_timestamp(now_utc))
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        slots: list[dict[str, object]] = []
        for index in range(FORECAST_SLOT_COUNT):
            slot_start = start_local + timedelta(minutes=SLOT_MINUTES * index)
            slots.append(
                {
                    "from": slot_start.strftime("%Y-%m-%dT%H:%M"),
                    "load": round(load_kw, 3),
                }
            )
        return slots

    def _forecast_slot_starts_utc(self, now_utc: datetime) -> list[datetime]:
        """Return the UTC start time for each slot in the current day anchor."""
        local_now = dt_util.as_local(self._normalize_utc_timestamp(now_utc))
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return [
            dt_util.as_utc(start_local + timedelta(minutes=SLOT_MINUTES * index))
            for index in range(FORECAST_SLOT_COUNT)
        ]

    def _load_historical_slot_rows_sync(
        self, now_utc: datetime | None = None
    ) -> list[_HistoricalSlotRow]:
        """Load retained slot rows used to fit the learned baseline model."""
        db = self._ensure_db()
        cutoff_utc = self._format_utc_timestamp(
            self._normalize_utc_timestamp(now_utc)
            - timedelta(days=FORECAST_HISTORY_WINDOW_DAYS)
        )
        rows = db.execute(
            "SELECT slot_start_utc, load_kw, sample_count, quality_score, features_json "
            "FROM slot_rows WHERE slot_start_utc >= ? ORDER BY slot_start_utc ASC",
            (cutoff_utc,),
        ).fetchall()
        history_rows: list[_HistoricalSlotRow] = []
        for row in rows:
            slot_start_utc = self._parse_utc_timestamp(str(row[0]))
            features: Mapping[str, Any] = {}
            features_json = row[4]
            if isinstance(features_json, str) and features_json:
                try:
                    parsed_features = json.loads(features_json)
                except json.JSONDecodeError:
                    parsed_features = {}
                if isinstance(parsed_features, Mapping):
                    features = parsed_features
            slot_index_value = features.get("slot_index")
            try:
                slot_index = int(slot_index_value)
            except (TypeError, ValueError):
                slot_index = self._slot_index(slot_start_utc)
            day_type_value = features.get("day_type")
            day_type = (
                str(day_type_value)
                if isinstance(day_type_value, str) and day_type_value
                else self._slot_day_type(slot_start_utc)
            )
            is_holiday = bool(features.get("is_holiday", False))
            history_rows.append(
                _HistoricalSlotRow(
                    slot_start_utc=slot_start_utc,
                    load_kw=float(row[1]),
                    quality_score=float(row[3] or 0.0),
                    sample_count=int(row[2] or 0),
                    slot_index=slot_index,
                    day_type=day_type,
                    is_holiday=is_holiday,
                )
            )
        return history_rows

    def _build_forecast_summary(
        self, history_rows: list[_HistoricalSlotRow]
    ) -> _ForecastSummary:
        """Summarize the retained history for the sensor attributes."""
        if len(history_rows) < FORECAST_COLD_START_MIN_FINALIZED_SLOTS:
            return _ForecastSummary()

        local_dates = [
            dt_util.as_local(row.slot_start_utc).date() for row in history_rows
        ]
        unique_dates = sorted(set(local_dates))
        last_sample = history_rows[-1]
        learning_nights = len(unique_dates)
        if learning_nights < FORECAST_MIN_HISTORY_DAYS:
            reason = (
                "Household forecast is warming up; using a recency-weighted "
                f"baseline from {learning_nights} learned days of slot history."
            )
        else:
            reason = (
                "Household forecast is using a seasonal baseline learned from "
                f"{learning_nights} days of slot history."
            )

        return _ForecastSummary(
            learning_nights=learning_nights,
            data_since=unique_dates[0].isoformat(),
            last_sample_date=dt_util.as_local(last_sample.slot_start_utc).date().isoformat(),
            last_sample_kw=last_sample.load_kw,
            reason=reason,
        )

    def _load_recent_closed_slot_rows_sync(
        self, now_utc: datetime
    ) -> list[_HistoricalSlotRow]:
        """Load the most recent closed slots for the current local day."""
        db = self._ensure_db()
        local_now = dt_util.as_local(self._normalize_utc_timestamp(now_utc))
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_utc = dt_util.as_utc(local_start)
        current_slot_start_utc = self._floor_to_slot_start_utc(now_utc)
        rows = db.execute(
            "SELECT slot_start_utc, load_kw, sample_count, quality_score, features_json "
            "FROM slot_rows WHERE slot_start_utc >= ? AND slot_start_utc < ? "
            "ORDER BY slot_start_utc ASC",
            (
                self._format_utc_timestamp(day_start_utc),
                self._format_utc_timestamp(current_slot_start_utc),
            ),
        ).fetchall()

        recent_rows: list[_HistoricalSlotRow] = []
        for row in rows:
            slot_start_utc = self._parse_utc_timestamp(str(row[0]))
            features: Mapping[str, Any] = {}
            features_json = row[4]
            if isinstance(features_json, str) and features_json:
                try:
                    parsed_features = json.loads(features_json)
                except json.JSONDecodeError:
                    parsed_features = {}
                if isinstance(parsed_features, Mapping):
                    features = parsed_features

            slot_index_value = features.get("slot_index")
            try:
                slot_index = int(slot_index_value)
            except (TypeError, ValueError):
                slot_index = self._slot_index(slot_start_utc)

            day_type_value = features.get("day_type")
            day_type = (
                str(day_type_value)
                if isinstance(day_type_value, str) and day_type_value
                else self._slot_day_type(slot_start_utc)
            )
            is_holiday = bool(features.get("is_holiday", False))
            recent_rows.append(
                _HistoricalSlotRow(
                    slot_start_utc=slot_start_utc,
                    load_kw=float(row[1]),
                    quality_score=float(row[3] or 0.0),
                    sample_count=int(row[2] or 0),
                    slot_index=slot_index,
                    day_type=day_type,
                    is_holiday=is_holiday,
                )
            )
        return recent_rows[-RESIDUAL_APPLY_HORIZON_SLOTS:]

    def _compute_residual_correction_kw_sync(
        self,
        now_utc: datetime,
        *,
        previous_forecast_slots: list[dict[str, object]] | None = None,
        learning_nights: int = 0,
    ) -> float:
        """Return the additive correction for the next few forecast slots."""
        if learning_nights < FORECAST_MIN_HISTORY_DAYS:
            return 0.0

        recent_rows = self._load_recent_closed_slot_rows_sync(now_utc)
        if len(recent_rows) < RESIDUAL_MIN_CONSECUTIVE_SLOTS:
            return 0.0

        current_slot_start_utc = self._floor_to_slot_start_utc(now_utc)
        latest_closed_end_utc = recent_rows[-1].slot_start_utc + timedelta(
            minutes=SLOT_MINUTES
        )
        if (
            current_slot_start_utc - latest_closed_end_utc
        ).total_seconds() >= RESIDUAL_MAX_STALE_MINUTES * 60:
            return 0.0

        if previous_forecast_slots is None:
            previous_forecast_slots = self._forecast_slots
        if not previous_forecast_slots:
            return 0.0

        previous_forecast_by_from: dict[str, float] = {}
        for slot in previous_forecast_slots:
            if not isinstance(slot, Mapping):
                continue
            slot_from = slot.get("from")
            if not isinstance(slot_from, str) or not slot_from:
                continue
            try:
                previous_forecast_by_from[slot_from] = float(slot.get("load", 0.0))
            except (TypeError, ValueError):
                continue
        if not previous_forecast_by_from:
            return 0.0

        residual_tail: list[float] = []
        previous_row_start: datetime | None = None
        for row in reversed(recent_rows):
            if row.sample_count <= 0 or row.quality_score < 0.5:
                break
            slot_from_local = dt_util.as_local(row.slot_start_utc).strftime(
                "%Y-%m-%dT%H:%M"
            )
            previous_load = previous_forecast_by_from.get(slot_from_local)
            if previous_load is None:
                break
            if (
                previous_row_start is not None
                and previous_row_start - row.slot_start_utc
                != timedelta(minutes=SLOT_MINUTES)
            ):
                break

            residual_kw = row.load_kw - previous_load
            if abs(residual_kw) < RESIDUAL_ACTIVATION_THRESHOLD_KW:
                break

            residual_tail.append(residual_kw)
            previous_row_start = row.slot_start_utc
            if len(residual_tail) >= RESIDUAL_APPLY_HORIZON_SLOTS:
                break

        if len(residual_tail) < RESIDUAL_MIN_CONSECUTIVE_SLOTS:
            return 0.0

        residual_tail.reverse()
        correction_kw = residual_tail[0]
        for residual_kw in residual_tail[1:]:
            correction_kw = (
                RESIDUAL_ALPHA * residual_kw
                + (1.0 - RESIDUAL_ALPHA) * correction_kw
            )

        return max(
            -RESIDUAL_MAX_CORRECTION_KW,
            min(RESIDUAL_MAX_CORRECTION_KW, correction_kw),
        )

    def _apply_residual_correction_to_forecast_slots(
        self,
        forecast_slots: list[dict[str, object]],
        slot_starts_utc: list[datetime],
        now_utc: datetime,
        correction_kw: float,
    ) -> list[dict[str, object]]:
        """Apply a short-lived correction to the next few open slots."""
        if not forecast_slots or not slot_starts_utc or correction_kw == 0.0:
            return [dict(slot) for slot in forecast_slots]

        current_slot_start_utc = self._floor_to_slot_start_utc(now_utc)
        corrected_slots: list[dict[str, object]] = []
        for slot, slot_start_utc in zip(forecast_slots, slot_starts_utc):
            try:
                base_load_kw = float(slot["load"])
            except (TypeError, ValueError, KeyError):
                corrected_slots.append(dict(slot))
                continue

            if slot_start_utc < current_slot_start_utc:
                corrected_slots.append(dict(slot))
                continue

            slots_ahead = int(
                (slot_start_utc - current_slot_start_utc).total_seconds()
                // (SLOT_MINUTES * 60)
            )
            if slots_ahead >= RESIDUAL_APPLY_HORIZON_SLOTS:
                corrected_load_kw = base_load_kw
            else:
                corrected_load_kw = max(
                    0.0,
                    base_load_kw
                    + correction_kw * (RESIDUAL_DECAY_FACTOR**slots_ahead),
                )
            corrected_slots.append(
                {
                    "from": slot.get("from"),
                    "load": round(corrected_load_kw, 3),
                }
            )
        return corrected_slots

    def _build_forecast_slots_from_history_sync(
        self, now_utc: datetime
    ) -> tuple[list[dict[str, object]], _ForecastSummary]:
        """Build the current 192-slot forecast from retained slot history."""
        previous_forecast_slots = [dict(slot) for slot in self._forecast_slots]
        history_rows = self._load_historical_slot_rows_sync(now_utc)
        summary = self._build_forecast_summary(history_rows)
        if len(history_rows) < FORECAST_COLD_START_MIN_FINALIZED_SLOTS:
            forecast_slots = self._build_constant_forecast_slots(
                STATIC_LOAD_FORECAST_KW,
                now_utc,
            )
            return (
                self._preserve_historical_forecast_slots(
                    forecast_slots,
                    self._forecast_slot_starts_utc(now_utc),
                    now_utc,
                ),
                summary,
            )

        model_rows = history_rows
        max_model_rows = FORECAST_TRAINING_WINDOW_DAYS * FORECAST_SLOT_COUNT
        if len(model_rows) > max_model_rows:
            model_rows = model_rows[-max_model_rows:]

        row_count = len(model_rows)
        if row_count == 0:
            forecast_slots = self._build_constant_forecast_slots(
                self._load_forecast_kw,
                now_utc,
            )
            return (
                self._preserve_historical_forecast_slots(
                    forecast_slots,
                    self._forecast_slot_starts_utc(now_utc),
                    now_utc,
                ),
                summary,
            )

        weighted_stats: dict[tuple[str, int], _WeightedStats] = {}
        slot_stats: dict[int, _WeightedStats] = {}
        day_type_stats: dict[str, _WeightedStats] = {}
        global_stats = _WeightedStats()

        for row in model_rows:
            age_days = max(
                0.0,
                (now_utc - row.slot_start_utc).total_seconds() / 86400.0,
            )
            recency_weight = math.exp(
                -age_days / FORECAST_RECENCY_HALF_LIFE_DAYS
            )
            weight = max(0.0, row.quality_score) * recency_weight
            if weight <= 0.0:
                continue
            weighted_stats.setdefault((row.day_type, row.slot_index), _WeightedStats()).add(
                row.load_kw,
                weight,
            )
            slot_stats.setdefault(row.slot_index, _WeightedStats()).add(
                row.load_kw,
                weight,
            )
            day_type_stats.setdefault(row.day_type, _WeightedStats()).add(
                row.load_kw,
                weight,
            )
            global_stats.add(row.load_kw, weight)

        global_mean = global_stats.mean()
        if global_mean is None:
            global_mean = self._load_forecast_kw

        recent_rows = [row for row in model_rows if row.quality_score > 0.0]
        if len(recent_rows) >= FORECAST_TREND_WINDOW_SLOTS * 2:
            recent_window = recent_rows[-FORECAST_TREND_WINDOW_SLOTS :]
            previous_window = recent_rows[
                -FORECAST_TREND_WINDOW_SLOTS * 2 : -FORECAST_TREND_WINDOW_SLOTS
            ]
            recent_mean = sum(row.load_kw for row in recent_window) / len(recent_window)
            previous_mean = sum(row.load_kw for row in previous_window) / len(
                previous_window
            )
            trend_delta = recent_mean - previous_mean
        else:
            trend_delta = 0.0

        local_now = dt_util.as_local(now_utc)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        learned = summary.learning_nights >= FORECAST_MIN_HISTORY_DAYS
        forecast_slots: list[dict[str, object]] = []
        forecast_slot_starts_utc: list[datetime] = []

        for index in range(FORECAST_SLOT_COUNT):
            slot_start_local = start_local + timedelta(minutes=SLOT_MINUTES * index)
            slot_start_utc = dt_util.as_utc(slot_start_local)
            forecast_slot_starts_utc.append(slot_start_utc)
            day_type = self._slot_day_type(slot_start_utc)
            slot_index = self._slot_index(slot_start_utc)

            exact_mean = weighted_stats.get((day_type, slot_index))
            slot_mean = slot_stats.get(slot_index)
            day_mean = day_type_stats.get(day_type)
            if learned and exact_mean is not None and exact_mean.count >= 2:
                base_load_kw = exact_mean.mean()
            elif slot_mean is not None and slot_mean.mean() is not None:
                base_load_kw = slot_mean.mean()
            elif day_mean is not None and day_mean.mean() is not None:
                base_load_kw = day_mean.mean()
            else:
                base_load_kw = global_mean

            if base_load_kw is None:
                base_load_kw = self._load_forecast_kw

            if learned:
                trend_decay = math.exp(-index / 24.0)
                base_load_kw = max(
                    0.0,
                    base_load_kw + (trend_delta * 0.35 * trend_decay),
                )

            forecast_slots.append(
                {
                    "from": slot_start_local.strftime("%Y-%m-%dT%H:%M"),
                    "load": round(base_load_kw, 3),
                }
            )

        residual_correction_kw = self._compute_residual_correction_kw_sync(
            now_utc,
            previous_forecast_slots=previous_forecast_slots,
            learning_nights=summary.learning_nights,
        )
        if residual_correction_kw != 0.0:
            forecast_slots = self._apply_residual_correction_to_forecast_slots(
                forecast_slots,
                forecast_slot_starts_utc,
                now_utc,
                residual_correction_kw,
            )

        return (
            self._preserve_historical_forecast_slots(
                forecast_slots,
                forecast_slot_starts_utc,
                now_utc,
            ),
            summary,
        )

    def _cache_forecast_slots(
        self,
        forecast_slots: list[dict[str, object]],
        summary: _ForecastSummary,
    ) -> None:
        """Store the latest forecast and visible learning summary."""
        self._forecast_slots = [dict(slot) for slot in forecast_slots]
        self._forecast_summary = summary
        if forecast_slots:
            try:
                self._load_forecast_kw = float(forecast_slots[0]["load"])
            except (TypeError, ValueError, KeyError):
                self._load_forecast_kw = STATIC_LOAD_FORECAST_KW
        else:
            self._load_forecast_kw = STATIC_LOAD_FORECAST_KW

    def _preserve_historical_forecast_slots(
        self,
        forecast_slots: list[dict[str, object]],
        slot_starts_utc: list[datetime],
        now_utc: datetime,
    ) -> list[dict[str, object]]:
        """Keep already-published historical slots stable across refreshes."""
        if not forecast_slots:
            return []
        if len(forecast_slots) != len(slot_starts_utc):
            return [dict(slot) for slot in forecast_slots]
        if not self._forecast_slots or len(self._forecast_slots) != len(forecast_slots):
            return [dict(slot) for slot in forecast_slots]

        current_anchor_text = dt_util.as_local(
            self._normalize_utc_timestamp(now_utc)
        ).replace(hour=0, minute=0, second=0, microsecond=0).strftime(
            "%Y-%m-%dT%H:%M"
        )
        first_existing = self._forecast_slots[0]
        previous_anchor = (
            str(first_existing.get("from", ""))
            if isinstance(first_existing, Mapping)
            else ""
        )
        if previous_anchor != current_anchor_text:
            return [dict(slot) for slot in forecast_slots]

        current_slot_start_utc = self._floor_to_slot_start_utc(now_utc)
        merged_slots: list[dict[str, object]] = []
        for index, (slot, slot_start_utc) in enumerate(zip(forecast_slots, slot_starts_utc)):
            if slot_start_utc < current_slot_start_utc:
                previous_slot = self._forecast_slots[index]
                if isinstance(previous_slot, Mapping):
                    merged_slots.append(dict(previous_slot))
                    continue
            merged_slots.append(dict(slot))
        return merged_slots

    def _persist_interval_energy_rows_sync(
        self, interval_rows: list[_IntervalEnergyRow]
    ) -> None:
        """Store the rebuilt interval-energy rows in SQLite."""
        if not interval_rows:
            return
        db = self._ensure_db()
        db.executemany(
            "INSERT OR REPLACE INTO interval_energy "
            "(ts_from_utc, ts_to_utc, sensor_key, delta_kwh, interval_sec, "
            "interval_kw, quality) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    self._format_utc_timestamp(row.ts_from_utc),
                    self._format_utc_timestamp(row.ts_to_utc),
                    CONF_POWER_METER_CONSUMPTION,
                    row.delta_kwh,
                    row.interval_sec,
                    row.interval_kw,
                    row.quality,
                )
                for row in interval_rows
            ],
        )

    def _create_slot_accumulators(
        self,
        rebuild_start_slot_utc: datetime,
        latest_closed_slot_utc: datetime,
    ) -> tuple[list[_SlotAccumulator], dict[datetime, _SlotAccumulator]]:
        """Create the closed-slot grid used by interval allocation."""
        slot_accumulators: list[_SlotAccumulator] = []
        slot_lookup: dict[datetime, _SlotAccumulator] = {}

        slot_start = rebuild_start_slot_utc
        while slot_start <= latest_closed_slot_utc:
            accumulator = _SlotAccumulator(slot_start_utc=slot_start)
            slot_accumulators.append(accumulator)
            slot_lookup[slot_start] = accumulator
            slot_start += timedelta(minutes=SLOT_MINUTES)

        return slot_accumulators, slot_lookup

    def _allocate_interval_rows_to_slots(
        self,
        interval_rows: list[_IntervalEnergyRow],
        slot_lookup: dict[datetime, _SlotAccumulator],
        rebuild_start_slot_utc: datetime,
        latest_closed_slot_utc: datetime,
        *,
        energy_attr: str,
        track_sample_counts: bool,
    ) -> None:
        """Allocate interval energy rows into the supplied slot grid."""
        if not interval_rows or not slot_lookup:
            return

        slot_duration = timedelta(minutes=SLOT_MINUTES)
        for interval in interval_rows:
            interval_start = interval.ts_from_utc
            interval_end = interval.ts_to_utc
            if interval_end <= interval_start:
                continue

            first_slot = max(
                rebuild_start_slot_utc, self._floor_to_slot_start_utc(interval_start)
            )
            last_slot = min(
                latest_closed_slot_utc,
                self._floor_to_slot_start_utc(interval_end - timedelta(microseconds=1)),
            )
            if last_slot < first_slot:
                continue

            slot_start = first_slot
            while slot_start <= last_slot:
                slot_end = slot_start + slot_duration
                overlap_start = max(interval_start, slot_start)
                overlap_end = min(interval_end, slot_end)
                overlap_seconds = (overlap_end - overlap_start).total_seconds()
                if overlap_seconds > 0:
                    accumulator = slot_lookup[slot_start]
                    allocated_kwh = interval.delta_kwh * (
                        overlap_seconds / interval.interval_sec
                    )
                    setattr(
                        accumulator,
                        energy_attr,
                        getattr(accumulator, energy_attr) + allocated_kwh,
                    )
                    if track_sample_counts:
                        accumulator.observed_seconds += overlap_seconds
                        accumulator.sample_count += 1
                        if interval.quality == "sparse_gap":
                            accumulator.sparse_interval_count += 1
                slot_start += slot_duration

    def _build_slot_accumulators(
        self,
        interval_rows: list[_IntervalEnergyRow],
        rebuild_start_slot_utc: datetime,
        latest_closed_slot_utc: datetime,
    ) -> list[_SlotAccumulator]:
        """Allocate interval energy across closed 15-minute slots."""
        slot_accumulators, slot_lookup = self._create_slot_accumulators(
            rebuild_start_slot_utc,
            latest_closed_slot_utc,
        )
        if not slot_accumulators:
            return slot_accumulators

        self._allocate_interval_rows_to_slots(
            interval_rows,
            slot_lookup,
            rebuild_start_slot_utc,
            latest_closed_slot_utc,
            energy_attr="slot_energy_kwh",
            track_sample_counts=True,
        )
        return slot_accumulators

    def _persist_slot_rows_sync(
        self, slot_accumulators: list[_SlotAccumulator], rebuild_start_slot_utc: datetime
    ) -> None:
        """Replace the rebuilt slot rows for the finalized window."""
        if not slot_accumulators:
            return
        db = self._ensure_db()
        latest_closed_slot_utc = slot_accumulators[-1].slot_start_utc
        db.execute(
            "DELETE FROM slot_rows WHERE slot_start_utc >= ? AND slot_start_utc <= ?",
            (
                self._format_utc_timestamp(rebuild_start_slot_utc),
                self._format_utc_timestamp(latest_closed_slot_utc),
            ),
        )
        db.executemany(
            "INSERT OR REPLACE INTO slot_rows "
            "(slot_start_utc, slot_energy_kwh, load_kw, sample_count, "
            "quality_score, features_json) VALUES (?, ?, ?, ?, ?, ?)",
            [
                self._build_slot_row(accumulator.slot_start_utc, accumulator)
                for accumulator in slot_accumulators
            ],
        )

    def _persist_forecast_runs_sync(self, generated_at_utc: datetime) -> None:
        """Store the current shell forecast in the forecast history table."""
        db = self._ensure_db()
        forecast_rows: list[tuple[str, str, float]] = []
        for forecast_slot in self._forecast_slots:
            slot_from_text = str(forecast_slot.get("from", ""))
            slot_load = float(forecast_slot.get("load", self.load_forecast_kw))
            slot_from_local = datetime.fromisoformat(slot_from_text)
            if slot_from_local.tzinfo is None:
                slot_from_local = slot_from_local.replace(
                    tzinfo=dt_util.now().tzinfo or timezone.utc
                )
            slot_start_utc = slot_from_local.astimezone(timezone.utc)
            forecast_rows.append(
                (
                    self._format_utc_timestamp(generated_at_utc),
                    self._format_utc_timestamp(slot_start_utc),
                    slot_load,
                )
            )

        db.executemany(
            "INSERT OR REPLACE INTO forecast_runs "
            "(generated_at_utc, slot_start_utc, load_kw) VALUES (?, ?, ?)",
            forecast_rows,
        )

    def _set_meta_timestamp_sync(self, key: str, timestamp: datetime) -> None:
        """Store a UTC timestamp in the meta table."""
        db = self._ensure_db()
        db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, self._format_utc_timestamp(timestamp)),
        )

    def _rebuild_history_sync(self, now_utc: datetime) -> bool:
        """Rebuild interval energy, slot rows, and forecast history."""
        samples = self._load_meter_samples_sync()
        interval_rows = self._build_interval_energy_rows(samples)
        appliance_rows = self._build_appliance_subtraction_rows_sync()

        db = self._ensure_db()
        updated = False
        if interval_rows:
            self._persist_interval_energy_rows_sync(interval_rows)
            latest_closed_slot_utc = self._floor_to_slot_start_utc(now_utc) - timedelta(
                minutes=SLOT_MINUTES
            )

            rebuild_start_slot_utc: datetime | None = None
            earliest_interval_slot_utc = self._floor_to_slot_start_utc(
                interval_rows[0].ts_from_utc
            )
            rebuild_start_slot_utc = earliest_interval_slot_utc
            if self._last_finalized_slot_utc is not None:
                rebuild_start_slot_utc = min(
                    rebuild_start_slot_utc,
                    self._floor_to_slot_start_utc(self._last_finalized_slot_utc)
                    - timedelta(minutes=SLOT_MINUTES),
                )

            if rebuild_start_slot_utc <= latest_closed_slot_utc:
                slot_accumulators = self._build_slot_accumulators(
                    interval_rows,
                    rebuild_start_slot_utc,
                    latest_closed_slot_utc,
                )
                if slot_accumulators:
                    if appliance_rows:
                        slot_lookup = {
                            accumulator.slot_start_utc: accumulator
                            for accumulator in slot_accumulators
                        }
                        self._allocate_interval_rows_to_slots(
                            appliance_rows,
                            slot_lookup,
                            rebuild_start_slot_utc,
                            latest_closed_slot_utc,
                            energy_attr="subtracted_energy_kwh",
                            track_sample_counts=False,
                        )
                    self._persist_slot_rows_sync(
                        slot_accumulators,
                        rebuild_start_slot_utc,
                    )
                    self._last_finalized_slot_utc = latest_closed_slot_utc
                    self._set_meta_timestamp_sync(
                        "last_finalized_slot_utc",
                        latest_closed_slot_utc,
                    )
                    updated = True

        forecast_slots, summary = self._build_forecast_slots_from_history_sync(now_utc)
        self._cache_forecast_slots(forecast_slots, summary)
        self._persist_forecast_runs_sync(now_utc)
        self._last_generation_utc = now_utc
        self._set_meta_timestamp_sync("last_generation_utc", now_utc)
        updated = True

        if updated:
            db.commit()
        return updated

    async def _async_finalize_history(self, now_utc: datetime | None = None) -> bool:
        """Rebuild the interval, slot, and forecast history for the meter."""
        finalize_now = self._normalize_utc_timestamp(now_utc)
        async with self._db_write_lock:
            return await self.hass.async_add_executor_job(
                self._rebuild_history_sync,
                finalize_now,
            )

    async def _async_refresh_forecast(self, now_utc: datetime | None = None) -> None:
        """Refresh the learned forecast and notify listeners when it changes."""
        if await self._async_finalize_history(now_utc):
            self._notify_update()
