"""Solar Forecast sensor for the ElectricityPriceLevels integration."""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo

from ..const import (
    ATTR_DATA_SINCE,
    ATTR_ENERGY_TODAY_KWH,
    ATTR_ENERGY_TOMORROW_KWH,
    ATTR_FORECASTS,
    ATTR_INTRADAY_SCALING,
    ATTR_TOTAL_SAMPLES,
    CONF_EXCLUDE_FROM_RECORDING,
)
from ..solar_forecast_coordinator import SolarForecastCoordinator


class SolarForecastSensor(SensorEntity):
    """
    Refined solar forecast sensor — today and tomorrow.

    State   : corrected kW estimate for the current 15-minute slot
    Unit    : kW
    Attributes:
        forecasts           – list of 192 dicts covering today 00:00 – tomorrow 24:00 (local):
                                end                 local time string (YYYY-MM-DDTHH:MM)
                                pow                 kW  (corrected)
                                raw                 kW  (OM, uncorrected)
        total_samples       – how many (om, actual) pairs are stored
        data_since          – ISO date of oldest stored reading
        energy_today_kwh    – total corrected kWh for today (full calendar day)
        energy_tomorrow_kwh – total corrected kWh for tomorrow
        intraday_scaling    – real-time scaling factor applied to today's future slots
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "kW"
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({ATTR_FORECASTS})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        coordinator: SolarForecastCoordinator,
    ) -> None:
        self._entry = entry
        self._coordinator = coordinator
        description = SensorEntityDescription(
            key="solarforecast",
            translation_key="solarforecast",
        )
        self.entity_description = description
        self._attr_suggested_object_id = description.key
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = device_info
        self._attr_exclude_from_recording = entry.options.get(
            CONF_EXCLUDE_FROM_RECORDING, True
        )

        # Pre-computed cache — updated once per coordinator refresh so state
        # and attribute reads don't need to rescan the forecast between updates.
        self._cached_native_value: float = 0.0
        self._cached_attrs: dict[str, object] = {}

    # ── HA lifecycle ──────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self._coordinator.register_update_callback(self._handle_coordinator_update)
        self._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.unregister_update_callback(self._handle_coordinator_update)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Recompute cached values then notify HA only if output changed."""
        old_value = self._cached_native_value
        old_attrs = self._cached_attrs
        self._recompute_cache()
        if self._cached_native_value != old_value or self._cached_attrs != old_attrs:
            self.async_write_ha_state()

    # ── Cache computation ─────────────────────────────────────────────────────

    def _recompute_cache(self) -> None:
        """
        Compute native_value and all extra_state_attributes in a single pass
        over the forecast list (15-minute resolution).
        """
        forecasts = self._coordinator.forecast
        local_tz = self._coordinator._local_tz
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(local_tz)
        today_local = now_local.date()

        today_kwh = 0.0
        tomorrow_kwh = 0.0
        current_pow_kw: float = 0.0

        for entry in forecasts:
            try:
                period_end = datetime.fromisoformat(entry["end"])
                if period_end.tzinfo is None:
                    period_end = period_end.replace(tzinfo=local_tz)
                period_date = period_end.date()
                pow_kw = entry["pow"]
                kwh = pow_kw * 0.25
                if period_date == today_local:
                    today_kwh += kwh
                elif period_date > today_local:
                    tomorrow_kwh += kwh
                # Current slot: the first slot whose end time is after now
                if current_pow_kw == 0.0 and period_end > now_local:
                    current_pow_kw = pow_kw
            except (ValueError, KeyError):
                continue

        self._cached_native_value = round(current_pow_kw, 3)
        self._cached_attrs = {
            ATTR_FORECASTS: forecasts,
            ATTR_ENERGY_TODAY_KWH: round(today_kwh, 2),
            ATTR_ENERGY_TOMORROW_KWH: round(tomorrow_kwh, 2),
            ATTR_TOTAL_SAMPLES: self._coordinator.total_samples,
            ATTR_DATA_SINCE: self._coordinator.data_since,
            ATTR_INTRADAY_SCALING: round(self._coordinator.intraday_scaling, 3),
        }

    # ── State & attributes ────────────────────────────────────────────────────

    @property
    def native_value(self) -> float:
        """Corrected kW estimate for the current 15-minute slot."""
        return self._cached_native_value

    @property
    def extra_state_attributes(self) -> dict:
        return self._cached_attrs
