"""Battery Charge Mode sensor.

The schedule mirrors the price sensor's slots (same start and end times),
copies each slot's cost and credit and uses a fixed target SoC.

Planner, first step: peaks and valleys, which alternate (valley, peak, valley,
peak, ...). A peak is the single highest slot after a valley; a valley is the
single lowest slot between two peaks (the latest one on a tie). For the first
peak there is no peak before it, so its valley is the lowest slot since
midnight of the peak's day. Slots are tried as peaks from the highest price rank
downwards and kept only while every peak is at least the cost margin
(``battery_degradation_cost``) more expensive than its valley. Each local day
has at most one peak from midnight to noon and at most one from noon to
midnight. Peaks get ``discharge``, valleys get ``charge``; every other slot
stays ``standby``.
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo

from ..const import (
    CONF_BATTERY_DEGRADATION_COST,
    CONF_EXCLUDE_FROM_RECORDING,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)

if TYPE_CHECKING:
    from .price import PriceSensor

_LOGGER = logging.getLogger(__name__)

MODE_STANDBY = "standby"
MODE_CHARGE = "charge"
MODE_DISCHARGE = "discharge"
DEFAULT_TARGET_SOC_PCT = 50
SLOT_TIME_FORMAT = "%Y-%m-%dT%H:%M"
COST_EPSILON = 1e-9


class BatteryChargeModeSensor(SensorEntity):
    """Battery charge mode sensor."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:battery-off"
    _unrecorded_attributes = frozenset({"modes"})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        price_sensor: PriceSensor,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._price_sensor = price_sensor
        self._modes: list[dict[str, str | float]] = []

        description = SensorEntityDescription(
            key="batterychargemode",
            translation_key="batterychargemode",
        )
        self.entity_description = description
        self.entity_id = PREFERRED_SENSOR_ENTITY_IDS[description.key]
        self._attr_suggested_object_id = description.key
        self._attr_unique_id = build_sensor_unique_id(entry, description.key)
        self._attr_device_info = device_info
        self._attr_exclude_from_recording = entry.options.get(
            CONF_EXCLUDE_FROM_RECORDING, True
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._modes = self._build_modes()
        self.async_on_remove(
            self._price_sensor.async_add_update_listener(self._handle_price_update)
        )

    @callback
    def _handle_price_update(self) -> None:
        """Rebuild the schedule whenever the price slots change."""
        self._modes = self._build_modes()
        self.async_write_ha_state()

    def _cost_margin(self) -> float:
        """Return the configured cost margin, or 0 when unset or invalid."""
        value = self._entry.options.get(CONF_BATTERY_DEGRADATION_COST)
        try:
            return max(float(value), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _plan_modes(self, rates: list[dict]) -> list[str]:
        """Return one mode per slot: ``discharge`` peaks, ``charge`` valleys.

        ``rates`` must be in chronological order. Peak candidates are tried
        from the highest rank down (ties: highest cost first, then earliest)
        and kept when the resulting peaks and valleys are still all valid and
        no peak has been kept yet in the same half day (see
        ``_half_day``). Adding a peak can only shrink the slot ranges the
        valleys are picked from, so a rejected candidate never becomes valid
        later. Slots without a numeric rank are never peaks but can still be
        valleys.
        """
        plan = [MODE_STANDBY] * len(rates)
        costs = [rate.get("cost", 0) for rate in rates]
        margin = self._cost_margin()

        candidates = sorted(
            (i for i, rate in enumerate(rates) if _is_number(rate.get("rank"))),
            key=lambda i: (-rates[i]["rank"], -costs[i], i),
        )
        peaks: list[int] = []
        valleys: list[int] = []
        peak_half_days: set[tuple[datetime.date, bool]] = set()
        for candidate in candidates:
            half_day = _half_day(rates[candidate])
            if half_day in peak_half_days:
                continue
            trial = sorted([*peaks, candidate])
            trial_valleys = self._find_valleys(rates, costs, trial, margin)
            if trial_valleys is not None:
                peaks, valleys = trial, trial_valleys
                peak_half_days.add(half_day)

        for peak in peaks:
            plan[peak] = MODE_DISCHARGE
        for valley in valleys:
            plan[valley] = MODE_CHARGE
        return plan

    @staticmethod
    def _find_valleys(
        rates: list[dict], costs: list[float], peaks: list[int], margin: float
    ) -> list[int] | None:
        """Return the valley slot before each peak, or ``None`` if one is invalid.

        ``peaks`` is sorted. The valley of a peak is the lowest slot between
        the previous peak and this one (the latest on equal cost). For the
        first peak it is the lowest slot since midnight of the peak's day. A
        peak is invalid when that range is empty or its valley is not at least
        ``margin`` cheaper (and strictly cheaper, even when the margin is 0).
        """
        valleys: list[int] = []
        for position, peak in enumerate(peaks):
            if position:
                first = peaks[position - 1] + 1
            else:
                first = _first_slot_of_day(rates, peak)
            if first >= peak:
                return None
            valley = min(range(first, peak), key=lambda i: (costs[i], -i))
            spread = costs[peak] - costs[valley]
            if spread <= COST_EPSILON or spread < margin - COST_EPSILON:
                return None
            valleys.append(valley)
        return valleys

    def _build_modes(self) -> list[dict[str, str | float]]:
        """Return one entry per price slot with its planned mode.

        Times, cost and credit are taken from the price slot; cost and credit
        are rounded to 2 decimals.
        """
        rates = [
            rate
            for rate in getattr(self._price_sensor, "_rates", None) or []
            if rate.get("start") is not None and rate.get("end") is not None
        ]
        plan = self._plan_modes(rates)
        return [
            {
                "from": rate["start"].strftime(SLOT_TIME_FORMAT),
                "to": rate["end"].strftime(SLOT_TIME_FORMAT),
                "mode": mode,
                "target_soc": DEFAULT_TARGET_SOC_PCT,
                "cost": round(rate.get("cost", 0), 2),
                "credit": round(rate.get("credit", 0), 2),
            }
            for rate, mode in zip(rates, plan)
        ]

    @property
    def state(self) -> str:
        return MODE_STANDBY

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"modes": self._modes}


def _is_number(value: object) -> bool:
    """Return whether ``value`` is a real number (ranks may be ``"N/A"``)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _first_slot_of_day(rates: list[dict], index: int) -> int:
    """Return the index of the first slot on the same local day as ``index``."""
    day = rates[index]["start"].date()
    first = index
    while first > 0 and rates[first - 1]["start"].date() == day:
        first -= 1
    return first


def _half_day(rate: dict) -> tuple[datetime.date, bool]:
    """Return the local day and whether the slot starts before noon."""
    start = rate["start"]
    return start.date(), start.hour < 12
