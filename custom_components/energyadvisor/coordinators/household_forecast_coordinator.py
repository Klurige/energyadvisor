"""Coordinator for the household Load forecast sensor.

The learning logic is intentionally disabled while the household forecast
feature is rebuilt. The coordinator keeps lifecycle housekeeping, exposes a
fixed 0.5 kW profile, and persists raw capture history in SQLite so later
steps can rebuild the forecast from stored samples.
"""

from __future__ import annotations

import asyncio
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
    CONF_CENTRAL_HEATING_ACTIVE_ENTITY,
    CONF_POWER_METER_CONSUMPTION,
    CONF_WATER_HEATER_ACTIVE_ENTITY,
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
STATIC_LOAD_FORECAST_W = 500.0
STATIC_LOAD_FORECAST_KW = STATIC_LOAD_FORECAST_W / 1000.0
STATIC_REASON = (
    "Household forecast learning is disabled; using a fixed 500 W profile."
)
RAW_REQUIRED_POLL_INTERVAL = timedelta(seconds=60)
RAW_OPTIONAL_POLL_INTERVAL = timedelta(seconds=120)
RAW_HEARTBEAT_SECONDS = 120.0
RAW_SAMPLE_RETENTION_DAYS = 21
SLOT_ROW_RETENTION_DAYS = 180
FORECAST_RUN_RETENTION_DAYS = 14
_NUMERIC_STATE_PATTERN = re.compile(r"[-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+)")
_ACTIVE_EVENT_STATES = {"on", "true", "active", "heating", "home"}
_INACTIVE_EVENT_STATES = {"off", "false", "inactive", "idle", "standby"}


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


class HouseholdForecastCoordinator:
    """Expose a fixed household load forecast profile and keep housekeeping."""

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

    # -- Public forecast values ------------------------------------------

    @property
    def load_forecast_kw(self) -> float:
        """Return the fixed household load forecast."""
        return self._load_forecast_kw

    @property
    def base_load_kw(self) -> float:
        """Backward-compatible alias for load forecast in kW."""
        return self.load_forecast_kw

    @property
    def household_load_forecast_w(self) -> float:
        """Return the fixed household load forecast in watts."""
        return self._load_forecast_kw * 1000.0

    @property
    def household_base_load_w(self) -> float:
        """Backward-compatible alias for load forecast in watts."""
        return self.household_load_forecast_w

    @property
    def forecast_slots(self) -> list[dict[str, object]]:
        """Return fixed 15-minute load slots for the 48-hour horizon."""
        start_local = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
        slots: list[dict[str, object]] = []
        for index in range(FORECAST_SLOT_COUNT):
            slot_start = start_local + timedelta(minutes=SLOT_MINUTES * index)
            slots.append(
                {
                    "from": slot_start.strftime("%Y-%m-%dT%H:%M"),
                    "load": self.load_forecast_kw,
                }
            )
        return slots

    @property
    def learning_nights(self) -> int:
        """Return the number of quiet-night samples retained."""
        return 0

    @property
    def data_since(self) -> str | None:
        """Return the oldest retained quiet-night sample date."""
        return None

    @property
    def last_sample_date(self) -> str | None:
        """Return the most recent quiet-night sample date."""
        return None

    @property
    def last_sample_kw(self) -> float | None:
        """Return the most recent quiet-night sample in kW."""
        return None

    @property
    def reason(self) -> str:
        """Return a human-readable status message."""
        return self._status_message

    @property
    def last_forecast_generation(self) -> str:
        """Return the timestamp of the last forecast shell generation."""
        return self._last_forecast_generation

    # -- Lifecycle -------------------------------------------------------

    async def async_setup(self) -> None:
        """Register listeners and load housekeeping state."""
        await self._async_load_state()
        if not (
            self._meter_entity
            and self._water_heater_entity
            and self._central_heating_entity
        ):
            _LOGGER.warning(
                "Household forecast cannot start because required entities are missing"
            )
            self._set_status(
                "Household forecast is waiting for the required meter and quiet-night sensors."
            )
            return

        self._capture_targets = self._build_capture_targets()
        await self._async_initialize_capture_db()

        self._listeners.append(
            async_track_state_change_event(
                self.hass,
                [self._water_heater_entity, self._central_heating_entity],
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
            "load_forecast_kw": self._load_forecast_kw,
        }

    def _restore_state(self, data: Mapping[str, Any]) -> bool:
        """Restore static housekeeping state from storage.

        Returns True when the payload should be normalized and persisted.
        """
        changed = False
        if data.get("mode") != STORE_MODE:
            changed = True

        restored_load_forecast = data.get("load_forecast_kw")
        if restored_load_forecast is None and "base_load_kw" in data:
            restored_load_forecast = data.get("base_load_kw")
            changed = True

        if restored_load_forecast is None:
            changed = True
        else:
            try:
                restored_value = float(restored_load_forecast)
            except (TypeError, ValueError):
                changed = True
            else:
                if restored_value != STATIC_LOAD_FORECAST_KW:
                    changed = True

        self._load_forecast_kw = STATIC_LOAD_FORECAST_KW
        return changed

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
        self._last_forecast_generation = dt_util.now().strftime("%Y-%m-%dT%H:%M")
        _LOGGER.info(
            "Household forecast refresh tick at %s: republishing the day-anchored shell",
            self._last_forecast_generation,
        )
        self._notify_update()

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

        for target in self._capture_targets.values():
            if target.is_event:
                row = db.execute(
                    "SELECT ts_utc, state_num, state_text FROM raw_events "
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
                "SELECT ts_utc, value, unit FROM raw_samples "
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

        prune_row = db.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("last_raw_prune_utc",),
        ).fetchone()
        if prune_row and prune_row[0]:
            self._last_raw_prune_utc = self._parse_utc_timestamp(str(prune_row[0]))

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
