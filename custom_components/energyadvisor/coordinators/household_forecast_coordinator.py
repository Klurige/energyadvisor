"""Coordinator for the household Load forecast sensor.

The learning logic is intentionally disabled while the household forecast
feature is rebuilt. The coordinator keeps lifecycle housekeeping and exposes a
static placeholder value to the sensor.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store

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
STATIC_LOAD_FORECAST_KW = 0.0
STATIC_REASON = (
    "Load forecast learning is disabled while the coordinator is being rebuilt."
)


class HouseholdForecastCoordinator:
    """Expose a static household Load forecast placeholder and keep housekeeping."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        self.hass = hass
        self.entry = entry

        self._listeners: list[Callable[[], None]] = []
        self._update_callbacks: list[Callable[[], None]] = []
        self._load_forecast_kw = STATIC_LOAD_FORECAST_KW
        self._status_message: str = STATIC_REASON
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

    # -- Public forecast values ------------------------------------------

    @property
    def load_forecast_kw(self) -> float:
        """Return the static placeholder household load forecast."""
        return self._load_forecast_kw

    @property
    def base_load_kw(self) -> float:
        """Backward-compatible alias for load forecast in kW."""
        return self.load_forecast_kw

    @property
    def household_load_forecast_w(self) -> float:
        """Return the static placeholder household load forecast in watts."""
        return self._load_forecast_kw * 1000.0

    @property
    def household_base_load_w(self) -> float:
        """Backward-compatible alias for load forecast in watts."""
        return self.household_load_forecast_w

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
        self._set_status(STATIC_REASON)

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
    def _handle_window_finish(self, _now=None) -> None:
        """Retained for structure while learning is disabled."""
        self._set_status(STATIC_REASON)
        self._schedule_state_save()
