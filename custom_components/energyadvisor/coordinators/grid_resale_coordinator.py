"""Live capture and explicit historical reconstruction for the resale ledger."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_point_in_utc_time,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..grid_resale import (
    MAX_SAMPLE_AGE,
    POWER_KEYS,
    ResaleLedger,
    STOCKHOLM,
    power_kw,
)

_LOGGER = logging.getLogger(__name__)


class GridResaleCoordinator(DataUpdateCoordinator[dict]):
    """Capture real measurements; never use the optimizer's intended modes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name="Grid resale", update_interval=timedelta(seconds=30)
        )
        self.entry = entry
        self.entities = {role: entry.options[key] for role, key in POWER_KEYS.items()}
        self.ledger = ResaleLedger(
            Path(
                hass.config.path(
                    "energyadvisor", f"grid_resale_{entry.entry_id}.sqlite3"
                )
            )
        )
        self._lock = asyncio.Lock()
        self._import_lock = asyncio.Lock()
        self._previous: tuple[datetime, dict] | None = None
        self._configuration_id = 0
        self._unsubscribe = None
        self._unsubscribe_year_reset = None
        self._stopped = False
        self.quality = "starting"

    async def async_setup(self) -> None:
        await self.hass.async_add_executor_job(self.ledger.initialize)
        self._configuration_id = await self.hass.async_add_executor_job(
            self.ledger.configuration,
            {"entities": self.entities, "positive_charge": True},
        )
        now = dt_util.utcnow()
        last_end = await self.hass.async_add_executor_job(self.ledger.latest_end)
        if last_end and last_end < now:
            await self.hass.async_add_executor_job(
                self.ledger.note_gap,
                last_end,
                now,
                self._configuration_id,
            )
        await self.async_refresh()

        @callback
        def changed(event) -> None:
            snapshot = self._snapshot()
            self.hass.async_create_task(
                self._async_capture_event(event.time_fired, snapshot)
            )

        self._unsubscribe = async_track_state_change_event(
            self.hass, list(self.entities.values()), changed
        )
        self._schedule_year_reset(now)

    def _schedule_year_reset(self, now: datetime) -> None:
        if self._unsubscribe_year_reset is not None:
            self._unsubscribe_year_reset()
        year = now.astimezone(STOCKHOLM).year
        boundary = datetime(year + 1, 1, 1, tzinfo=STOCKHOLM).astimezone(timezone.utc)
        self._unsubscribe_year_reset = async_track_point_in_utc_time(
            self.hass, self._async_new_year, boundary
        )

    async def _async_new_year(self, now: datetime) -> None:
        if self._stopped:
            return
        self._schedule_year_reset(now)
        await self._async_capture_event(now, self._snapshot())

    async def async_shutdown(self) -> None:
        self._stopped = True
        await super().async_shutdown()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._unsubscribe_year_reset is not None:
            self._unsubscribe_year_reset()
            self._unsubscribe_year_reset = None
        async with self._lock:
            self._previous = None

    def _snapshot(self) -> dict:
        return {
            role: self._state_evidence(self.hass.states.get(entity))
            for role, entity in self.entities.items()
        }

    @staticmethod
    def _state_evidence(state: State | None, historical: bool = False) -> dict:
        if state is None:
            return {"state": None, "unit": None, "reported_utc": None}
        reported = (
            state.last_updated
            if historical
            else getattr(state, "last_reported", state.last_updated)
        )
        return {
            "state": state.state,
            "unit": state.attributes.get("unit_of_measurement"),
            "reported_utc": reported.isoformat(),
        }

    def _values(
        self, snapshot: dict, end: datetime, check_age: bool = True
    ) -> dict | None:
        values = {}
        for role, reading in snapshot.items():
            value = power_kw(reading["state"], reading["unit"])
            if value is None or (role != "battery" and value < 0):
                return None
            reported = reading.get("reported_utc")
            if check_age and (
                not reported
                or not 0
                <= (end - datetime.fromisoformat(reported)).total_seconds()
                <= MAX_SAMPLE_AGE
            ):
                return None
            values[role] = value
        return values

    async def _async_update_data(self) -> dict:
        try:
            return await self._async_capture(dt_util.utcnow(), self._snapshot())
        except (OSError, ValueError, sqlite3.Error) as err:
            raise UpdateFailed(f"Grid resale capture failed: {err}") from err

    async def _async_capture_event(self, now: datetime, snapshot: dict) -> None:
        try:
            await self._async_capture(now, snapshot)
        except (OSError, ValueError, sqlite3.Error) as err:
            _LOGGER.exception("Grid resale capture failed")
            self.async_set_update_error(err)

    async def _async_capture(self, now: datetime, snapshot: dict) -> dict:
        async with self._lock:
            if self._stopped:
                return self.data or {}
            if self._previous is not None:
                start, previous = self._previous
                if now <= start:
                    _LOGGER.warning("Ignoring out-of-order resale sample at %s", now)
                    return self.data or {}
                values = self._values(previous, now)
                self.quality = "measured" if values is not None else "missing_or_stale"
                await self.hass.async_add_executor_job(
                    self.ledger.record,
                    start,
                    now,
                    values,
                    previous,
                    self._configuration_id,
                    "live",
                    self.quality,
                )
            self._previous = now, snapshot
            summary = await self.hass.async_add_executor_job(self.ledger.summary, now)
            summary["quality_status"] = self.quality
            self.async_set_updated_data(summary)
            return summary

    async def async_export(self) -> dict:
        """Create an authenticated, downloadable snapshot without stopping capture."""
        path = await self.hass.async_add_executor_job(
            self.ledger.export_csv,
            Path(self.hass.config.path("energyadvisor", "exports")),
        )
        return {
            "path": str(path),
            "download_url": f"/api/energyadvisor/grid_resale/{path.name}",
        }

    async def async_import_history(
        self, start: datetime, end: datetime, source: str
    ) -> dict:
        """Import bounded chunks; never replace retained live evidence."""
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("History requires increasing timezone-aware timestamps")
        if end > dt_util.utcnow():
            raise ValueError("Cannot import future history")
        if source not in ("raw", "hourly_estimate"):
            raise ValueError("History source must be raw or hourly_estimate")
        if self._import_lock.locked():
            raise ValueError("A grid resale historical import is already running")
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if source == "hourly_estimate" and any(
            stamp.minute or stamp.second or stamp.microsecond for stamp in (start, end)
        ):
            raise ValueError("Hourly imports require whole UTC hour boundaries")
        from homeassistant.components.recorder import get_instance

        inserted = skipped = empty_chunks = 0
        async with self._import_lock:
            cursor = start
            while cursor < end:
                if self._stopped:
                    raise ValueError(
                        "Grid resale history import interrupted by unload; rerun to resume."
                    )
                stop = min(end, cursor + timedelta(days=1 if source == "raw" else 31))
                loader = self._load_raw if source == "raw" else self._load_hourly
                rows = await get_instance(self.hass).async_add_executor_job(
                    loader, cursor, stop
                )
                if not rows:
                    empty_chunks += 1
                async with self._lock:
                    if self._stopped:
                        raise ValueError(
                            "Grid resale history import interrupted by unload; rerun to resume."
                        )
                    added, overlapping = await self.hass.async_add_executor_job(
                        self.ledger.record_many,
                        rows,
                        self._configuration_id,
                        f"history_{source}",
                    )
                    inserted += added
                    skipped += overlapping
                cursor = stop
            await self.hass.async_add_executor_job(
                self.ledger.log_import, start, end, source, inserted, skipped
            )
        await self.async_request_refresh()
        if empty_chunks:
            _LOGGER.warning(
                "Grid resale import found no usable data in %s chunks", empty_chunks
            )
        return {
            "inserted_intervals": inserted,
            "overlapping_intervals_skipped": skipped,
            "chunks_without_usable_data": empty_chunks,
            "source": source,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }

    def _load_hourly(self, start: datetime, end: datetime) -> list:
        from homeassistant.components.recorder.statistics import (
            get_metadata,
            statistics_during_period,
        )

        ids = set(self.entities.values())
        metadata = get_metadata(self.hass, statistic_ids=ids)
        units = {}
        for entity in ids:
            meta = metadata.get(entity)
            if meta is None or not meta[1].get("has_mean"):
                raise ValueError(f"No historical mean-power statistics for {entity}")
            units[entity] = meta[1].get("unit_of_measurement")
            if units[entity] not in ("W", "kW"):
                raise ValueError(f"Unsupported historical power unit for {entity}")
        statistics = statistics_during_period(
            self.hass, start, end, ids, "hour", None, {"mean"}
        )
        hours: dict[datetime, dict] = {}
        for role, entity in self.entities.items():
            for row in statistics.get(entity, []):
                stamp = row["start"]
                begin = (
                    datetime.fromtimestamp(stamp, timezone.utc)
                    if isinstance(stamp, (int, float))
                    else dt_util.as_utc(stamp)
                )
                if start <= begin and begin + timedelta(hours=1) <= end:
                    hours.setdefault(begin, {})[role] = {
                        "state": row.get("mean"),
                        "unit": units[entity],
                        "reported_utc": begin.isoformat(),
                    }
        return [
            (
                begin,
                begin + timedelta(hours=1),
                (
                    self._values(evidence, begin, False)
                    if set(evidence) == set(POWER_KEYS)
                    else None
                ),
                evidence,
                "hourly_estimate",
            )
            for begin, evidence in sorted(hours.items())
        ]

    def _load_raw(self, start: datetime, end: datetime) -> list:
        from homeassistant.components.recorder.history import get_significant_states

        history = get_significant_states(
            self.hass,
            start,
            end,
            list(self.entities.values()),
            significant_changes_only=False,
        )
        events = sorted(
            (
                (
                    state.last_updated,
                    role,
                    self._state_evidence(state, historical=True),
                )
                for role, entity in self.entities.items()
                for state in history.get(entity, [])
                if isinstance(state, State)
            ),
            key=lambda event: event[0],
        )
        snapshot: dict = {}
        rows = []
        cursor = start
        for stamp, role, reading in events:
            stamp = max(start, stamp)
            if stamp >= end:
                break
            if stamp > cursor and set(snapshot) == set(POWER_KEYS):
                values = self._values(snapshot, stamp)
                if values is not None:
                    rows.append(
                        (cursor, stamp, values, dict(snapshot), "raw_reconstruction")
                    )
            snapshot[role] = reading
            cursor = stamp
        if cursor < end and set(snapshot) == set(POWER_KEYS):
            values = self._values(snapshot, end)
            if values is not None:
                rows.append((cursor, end, values, snapshot, "raw_reconstruction"))
        return rows
