"""Coordinator for the household base-load forecast sensor.

The coordinator learns a rolling average from the household energy meter
between 01:00 and 04:00 on nights when the water heater and central heating
stay off for the full window. The learned value is exposed in kW so the
future battery planner can reserve enough energy for the next quiet night.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CENTRAL_HEATING_ACTIVE_ENTITY,
    CONF_POWER_METER_CONSUMPTION,
    CONF_WATER_HEATER_ACTIVE_ENTITY,
    parse_unit_of_measurement,
)

_LOGGER = logging.getLogger(__name__)

MAX_HISTORY_DAYS = 60
WINDOW_START_HOUR = 1
WINDOW_END_HOUR = 4
WINDOW_DURATION_HOURS = WINDOW_END_HOUR - WINDOW_START_HOUR
STORE_VERSION = 1


def _state_to_kwh(state: State | None) -> float | None:
    """Convert a cumulative energy sensor state to kWh."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None

    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None

    _, energy_unit = parse_unit_of_measurement(
        str(state.attributes.get("unit_of_measurement", ""))
    )
    if energy_unit is None:
        return value
    normalized_unit = energy_unit.lower()
    if normalized_unit == "wh":
        return value / 1000.0
    if normalized_unit == "mwh":
        return value * 1000.0
    return value


def _average_message(count: int) -> str:
    """Return a grammatically correct quiet-night average message."""
    noun = "night" if count == 1 else "nights"
    return f"Using the average of {count} quiet {noun}."


class HouseholdForecastCoordinator:
    """Learn and expose the household base-load forecast."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        self.hass = hass
        self.entry = entry

        self._listeners: list[Callable[[], None]] = []
        self._update_callbacks: list[Callable[[], None]] = []

        self._samples: dict[date, float] = {}
        self._window_date: date | None = None
        self._window_start_kwh: float | None = None
        self._window_invalid: bool = False
        self._window_skip_reason: str | None = None
        self._status_message: str = "Waiting for the first quiet-night sample."
        self._store = Store(
            hass,
            STORE_VERSION,
            f"energyadvisor_household_forecast_{entry.entry_id}",
        )

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

    # -- Public learned values -------------------------------------------

    @property
    def base_load_kw(self) -> float | None:
        """Return the current rolling average household base load."""
        if not self._samples:
            return None
        return sum(self._samples.values()) / len(self._samples)

    @property
    def household_base_load_w(self) -> float | None:
        """Return the current rolling average household base load in watts."""
        base_load_kw = self.base_load_kw
        if base_load_kw is None:
            return None
        return base_load_kw * 1000.0

    @property
    def learning_nights(self) -> int:
        """Return the number of valid quiet nights in the rolling average."""
        return len(self._samples)

    @property
    def data_since(self) -> str | None:
        """Return the ISO date of the oldest retained quiet-night sample."""
        if not self._samples:
            return None
        return min(self._samples).isoformat()

    @property
    def last_sample_date(self) -> str | None:
        """Return the ISO date of the most recent quiet-night sample."""
        if not self._samples:
            return None
        return max(self._samples).isoformat()

    @property
    def last_sample_kw(self) -> float | None:
        """Return the most recent quiet-night sample in kW."""
        if not self._samples:
            return None
        return self._samples[max(self._samples)]

    @property
    def reason(self) -> str:
        """Return a human-readable status message."""
        return self._status_message

    # -- Lifecycle -------------------------------------------------------

    async def async_setup(self) -> None:
        """Register listeners and schedule the quiet-night sampling window."""
        await self._async_load_state()
        if not (self._meter_entity and self._water_heater_entity and self._central_heating_entity):
            _LOGGER.warning(
                "Household forecast cannot start because required entities are missing"
            )
            self._status_message = (
                "Household forecast is waiting for the required meter and quiet-night sensors."
            )
            return

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

        if self._samples:
            self._status_message = _average_message(len(self._samples))

    async def async_shutdown(self) -> None:
        """Remove listeners and stop scheduling updates."""
        for remove in self._listeners:
            remove()
        self._listeners.clear()

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
        """Notify listeners that learned values changed."""
        for cb in tuple(self._update_callbacks):
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error notifying household forecast listeners")

    def _meter_value(self) -> float | None:
        """Read the current household meter value in kWh."""
        return _state_to_kwh(self.hass.states.get(self._meter_entity))

    def _quiet_sensors_off(self) -> bool:
        """Return True when both quiet-night sensors are currently off."""
        for entity_id in (self._water_heater_entity, self._central_heating_entity):
            state = self.hass.states.get(entity_id)
            if state is None or state.state != "off":
                return False
        return True

    def _prune_samples(self, today: date) -> bool:
        """Discard old samples outside the retention window."""
        cutoff = today - timedelta(days=MAX_HISTORY_DAYS)
        removed = False
        for sample_day in list(self._samples):
            if sample_day < cutoff:
                self._samples.pop(sample_day, None)
                removed = True
        return removed

    def _set_status(self, message: str) -> None:
        """Update the public status message and notify listeners when changed."""
        if message == self._status_message:
            return
        self._status_message = message
        self._notify_update()

    def _invalidate_window(self, reason: str) -> None:
        """Mark the current sampling window as invalid."""
        if self._window_invalid and self._window_skip_reason == reason:
            return
        self._window_invalid = True
        self._window_skip_reason = reason
        self._set_status(reason)
        self._schedule_state_save()

    def _serialize_state(self) -> dict[str, Any]:
        """Serialize learned samples and the current quiet-night window."""
        return {
            "samples": [
                {"date": sample_day.isoformat(), "base_load_kw": base_load_kw}
                for sample_day, base_load_kw in sorted(self._samples.items())
            ],
            "window": {
                "date": self._window_date.isoformat() if self._window_date else None,
                "start_kwh": self._window_start_kwh,
                "invalid": self._window_invalid,
                "skip_reason": self._window_skip_reason,
            },
        }

    def _restore_state(self, data: Mapping[str, Any]) -> bool:
        """Restore samples and window state from storage.

        Returns True when the restored payload needed pruning or normalization.
        """
        changed = False

        restored_samples: dict[date, float] = {}
        for item in data.get("samples", []):
            if not isinstance(item, Mapping):
                changed = True
                continue
            date_str = item.get("date")
            base_load_kw = item.get("base_load_kw")
            if date_str is None or base_load_kw is None:
                changed = True
                continue
            try:
                sample_day = date.fromisoformat(str(date_str))
                sample_kw = float(base_load_kw)
            except (TypeError, ValueError):
                changed = True
                continue
            restored_samples[sample_day] = sample_kw
        if restored_samples != self._samples:
            self._samples = restored_samples
            changed = True

        today = dt_util.now().date()
        removed_old_samples = self._prune_samples(today)
        if removed_old_samples:
            changed = True

        self._window_date = None
        self._window_start_kwh = None
        self._window_invalid = False
        self._window_skip_reason = None

        window = data.get("window")
        if isinstance(window, Mapping):
            window_date = window.get("date")
            try:
                restored_window_date = (
                    date.fromisoformat(str(window_date)) if window_date else None
                )
            except (TypeError, ValueError):
                restored_window_date = None
                changed = True

            if restored_window_date == today:
                start_kwh = window.get("start_kwh")
                try:
                    restored_start_kwh = (
                        float(start_kwh) if start_kwh is not None else None
                    )
                except (TypeError, ValueError):
                    restored_start_kwh = None
                    changed = True
                self._window_date = restored_window_date
                self._window_start_kwh = restored_start_kwh
                self._window_invalid = bool(window.get("invalid", False))
                skip_reason = window.get("skip_reason")
                self._window_skip_reason = (
                    str(skip_reason) if skip_reason is not None else None
                )
            elif restored_window_date is not None:
                changed = True

        self._update_status_from_state()
        return changed

    def _update_status_from_state(self) -> None:
        """Refresh the public status message from the current internal state."""
        if self._window_invalid and self._window_skip_reason:
            self._status_message = self._window_skip_reason
            return
        if self._window_date == dt_util.now().date() and self._window_start_kwh is not None:
            self._status_message = "Collecting today's 01:00-04:00 quiet-night sample."
            return
        if self._samples:
            self._status_message = _average_message(len(self._samples))
            return
        self._status_message = "Waiting for the first quiet-night sample."

    async def _async_load_state(self) -> None:
        """Load persisted samples and active window state from storage."""
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
        """Persist the current samples and quiet-night window."""
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
        """Capture the meter value at 01:00 and start the quiet-night window."""
        today = dt_util.now().date()

        if self._window_date == today and self._window_start_kwh is not None:
            return

        self._window_date = today
        self._window_start_kwh = self._meter_value()
        self._window_invalid = False
        self._window_skip_reason = None

        if self._window_start_kwh is None:
            self._invalidate_window(
                "The 01:00-04:00 quiet-night sample was skipped because the household meter was unavailable at 01:00."
            )
            return

        if not self._quiet_sensors_off():
            self._invalidate_window(
                "The 01:00-04:00 quiet-night sample was skipped because the water heater or central heating was active at 01:00."
            )
            return

        self._set_status("Collecting today's 01:00-04:00 quiet-night sample.")
        self._schedule_state_save()

    @callback
    def _handle_quiet_sensor_change(self, event) -> None:
        """Invalidate the current window if a quiet sensor turns on."""
        if self._window_date is None or self._window_start_kwh is None:
            return

        if dt_util.now().date() != self._window_date or self._window_invalid:
            return

        new_state = event.data.get("new_state")
        if new_state is None or new_state.state != "on":
            return

        self._invalidate_window(
            "The 01:00-04:00 quiet-night sample was skipped because the water heater or central heating turned on during the window."
        )

    @callback
    def _handle_window_finish(self, _now=None) -> None:
        """Finish the quiet-night window at 04:00 and store a new sample."""
        today = dt_util.now().date()
        if self._window_date != today:
            if self.learning_nights:
                self._set_status(_average_message(self.learning_nights))
            else:
                self._set_status("Waiting for the first quiet-night sample.")
            return

        end_kwh = self._meter_value()
        if end_kwh is None:
            self._invalidate_window(
                "The 01:00-04:00 quiet-night sample was skipped because the household meter was unavailable at 04:00."
            )
        elif not self._quiet_sensors_off():
            self._invalidate_window(
                "The 01:00-04:00 quiet-night sample was skipped because the water heater or central heating was active at 04:00."
            )
        elif self._window_start_kwh is not None and end_kwh < self._window_start_kwh:
            self._invalidate_window(
                "The household meter decreased during the 01:00-04:00 window, so the sample was discarded."
            )

        if not self._window_invalid and self._window_start_kwh is not None and end_kwh is not None:
            sample_kw = (end_kwh - self._window_start_kwh) / WINDOW_DURATION_HOURS
            self._samples[today] = sample_kw
            self._prune_samples(today)
            self._set_status(_average_message(len(self._samples)))
        elif self.learning_nights:
            self._set_status(_average_message(self.learning_nights))
        elif self._window_skip_reason is not None:
            self._set_status(self._window_skip_reason)
        else:
            self._set_status("Waiting for the first quiet-night sample.")

        self._window_date = None
        self._window_start_kwh = None
        self._window_invalid = False
        self._window_skip_reason = None
        self._schedule_state_save()
