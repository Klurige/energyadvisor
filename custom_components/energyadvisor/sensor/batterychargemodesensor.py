"""Battery Charge Mode sensor for Home Assistant.

Determines whether a home battery should be in standby, charging, or discharging
based on the electricity price schedule from the ElectricityPriceLevels sensor.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_DEGRADATION_COST,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_SOC_ENTITY,
    CONF_EXCLUDE_FROM_RECORDING,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)

if TYPE_CHECKING:
    from .electricitypricelevels import ElectricityPriceLevelsSensor

_LOGGER = logging.getLogger(__name__)

# Minimum sleep between periodic updates to prevent accidental tight loops.
_MIN_SLEEP_SECONDS = 5

# Tuning constants — fallback defaults when not configured.
_DEFAULT_MARGIN = (
    0.7  # Minimum price difference (cost units) to justify a charge/discharge cycle.
)
_DEFAULT_CHARGING_TIME_MINUTES = 160  # Time to fully charge the battery, in minutes.
_DEFAULT_DISCHARGING_TIME_MINUTES = (
    240  # Expected battery discharge duration, in minutes.
)
_DEFAULT_RESERVE_FRACTION = 0.05
_DEFAULT_CHARGE_EFFICIENCY = 0.95
_DEFAULT_DISCHARGE_EFFICIENCY = 0.95
_ENERGY_EPSILON_KWH = 0.05

_WAITING_FOR_RATES_REASON = "Waiting for electricity price data."
_OUTSIDE_HORIZON_REASON = (
    "Current time is outside the available battery schedule horizon."
)
_BETWEEN_WINDOWS_REASON = (
    "Current slot is outside the scheduled charge and discharge windows."
)
_NO_PROFITABLE_CYCLE_REASON = (
    "No profitable battery cycle is scheduled in the available price horizon."
)
_CHARGE_REASON = (
    "Charging is scheduled in a low-price window ahead of higher-price periods."
)
_DISCHARGE_REASON = (
    "Discharging is scheduled in a high-price window after earlier lower-price periods."
)
_DISCHARGE_BLOCKED_RESERVE_REASON = (
    "Discharging is blocked because the battery is already at its reserve floor."
)
_DISCHARGE_BLOCKED_ENERGY_REASON = "Discharging is blocked because the usable battery energy above reserve cannot cover the remaining slot."


def _format_compact_local_datetime(value: datetime) -> str:
    """Format a datetime like the other helper sensor attributes."""
    return value.strftime("%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------------------
# Algorithm (translated from JavaScript)
# ---------------------------------------------------------------------------


def _find_local_peaks(
    charge_entries, range_start, range_end, margin, total_peak_time_minutes
):
    """Mark the most expensive slots in a time window as 'discharge'.

    Finds the global price peak in [range_start, range_end), then widens it to
    fill up to total_peak_time_minutes of discharge time around that peak.
    Modifies charge_entries in place.
    """
    in_range = sorted(
        [e for e in charge_entries if range_start <= e["start"] < range_end],
        key=lambda e: e["cost"],
        reverse=True,
    )
    if not in_range:
        return

    peak = in_range[0]

    # Find the cheapest entry that lies before the peak in time.
    valley_index = len(in_range) - 1
    valley = in_range[valley_index]
    valley_index -= 1
    while valley["start"] > peak["start"] and valley_index > 0:
        valley = in_range[valley_index]
        valley_index -= 1

    if peak["cost"] < valley["cost"] + margin:
        return  # Price gap too small to justify discharging.

    peak_start = peak["start"]
    valley_start = valley["start"]
    if peak_start < valley_start:
        _LOGGER.debug(
            "Peak is before valley for range starting %s — skipping.", range_start
        )
        return

    slot_minutes = max(1, round((peak["end"] - peak["start"]).total_seconds() / 60))
    total_slots = round(total_peak_time_minutes / slot_minutes)
    slots_before = round(total_slots / 4)  # Reserve a quarter of slots before the peak.

    peaks = [peak]
    gaps = []  # High-cost slots between valley and peak when slots_before is exhausted.

    for candidate in in_range[1:]:
        if candidate["cost"] < valley["cost"] + margin or total_slots <= 0:
            break
        cdt = candidate["start"]
        if cdt > valley_start:
            if cdt < peak_start:
                if slots_before > 0:
                    peaks.append(candidate)
                    total_slots -= 1
                    slots_before -= 1
                else:
                    gaps.append(candidate)
            else:
                peaks.append(candidate)
                total_slots -= 1

    # Fill remaining slots from the overflow gap list.
    while total_slots > 0 and gaps:
        peaks.append(gaps.pop(0))
        total_slots -= 1

    if len(peaks) <= 2:
        return  # Too few slots to be meaningful.

    discharge_starts = {p["start"] for p in peaks}
    for entry in charge_entries:
        if entry["start"] in discharge_starts:
            entry["mode"] = "discharge"


def _find_local_valleys(charge_entries, margin, min_valley_time_minutes):
    """Mark the cheapest slots before each discharge block as 'charge'.

    For every discharge block, looks back up to 8 hours and picks the cheapest
    slots needed to cover min_valley_time_minutes of charging time.
    Modifies charge_entries in place.
    """
    discharge_entries = [e for e in charge_entries if e["mode"] == "discharge"]
    if not discharge_entries:
        return

    slot_minutes = max(
        1,
        round(
            (
                discharge_entries[0]["end"] - discharge_entries[0]["start"]
            ).total_seconds()
            / 60
        ),
    )
    min_slots = max(1, round(min_valley_time_minutes / slot_minutes))

    valley_starts: set = set()
    for i, peak in enumerate(discharge_entries):
        gap_end = peak["start"]
        gap_start = (
            charge_entries[0]["start"] if i == 0 else discharge_entries[i - 1]["end"]
        )
        gap_start = max(gap_start, gap_end - timedelta(hours=8))

        gap = sorted(
            [e for e in charge_entries if gap_start <= e["start"] < gap_end],
            key=lambda e: e["cost"],
        )
        if len(gap) >= min_slots:
            valley_starts.update(e["start"] for e in gap[:min_slots])

    for entry in charge_entries:
        if entry["start"] in valley_starts:
            entry["mode"] = "charge"


def _extend_peaks(charge_entries):
    """Extend discharge regions to cover the head, tail, and inter-block gaps.

    - Everything before the first non-standby entry becomes 'discharge'.
    - Everything after the last non-standby entry becomes 'discharge'.
    - Standby gaps between a discharge block and the following charge block
      become 'discharge'.
    Modifies charge_entries in place.
    """
    if not charge_entries:
        return

    # Head: entries before the first non-standby slot → discharge.
    first_ns = next((e for e in charge_entries if e["mode"] != "standby"), None)
    if first_ns is None:
        return
    head_start = charge_entries[0]["start"]
    head_end = first_ns["start"]
    for entry in charge_entries:
        if head_start <= entry["start"] < head_end:
            entry["mode"] = "discharge"

    # Tail: entries after the last non-standby slot → discharge.
    # Note: evaluated after the head step, so newly-set 'discharge' entries count.
    last_ns = next(
        (e for e in reversed(charge_entries) if e["mode"] != "standby"), None
    )
    tail_start = last_ns["end"] if last_ns else charge_entries[0]["start"]
    tail_end = charge_entries[-1]["end"]
    for entry in charge_entries:
        if tail_start <= entry["start"] < tail_end:
            entry["mode"] = "discharge"

    # Gaps: standby slots between a discharge block and its following charge block → discharge.
    non_standby = [e for e in charge_entries if e["mode"] != "standby"]
    for i in range(len(non_standby) - 1):
        cur = non_standby[i]
        nxt = non_standby[i + 1]
        if cur["mode"] == "discharge" and nxt["mode"] == "charge":
            gap_start = cur["end"]
            gap_end = nxt["start"]
            for entry in charge_entries:
                if gap_start < entry["start"] < gap_end:
                    entry["mode"] = "discharge"


def _parse_compact_rates(rates: list[dict]) -> list[dict]:
    """Parse compact rate dicts (from attributes) into datetime-based dicts.

    Compact format: {"from": "2026-05-25T00:00", "cost": 1.234, ...}
    Output format:  {"start": datetime, "end": datetime, "cost": float}
    """
    if not rates:
        return []

    local_tz = dt_util.get_default_time_zone()
    parsed = []
    for r in rates:
        from_str = r.get("from")
        if not from_str:
            continue
        start = datetime.fromisoformat(from_str).replace(tzinfo=local_tz)
        parsed.append({"start": start, "cost": r.get("cost", 0.0)})

    # Derive "end" from the next entry's start
    for i in range(len(parsed) - 1):
        parsed[i]["end"] = parsed[i + 1]["start"]
    if parsed:
        # Last entry: assume same duration as previous (or 60 min default)
        if len(parsed) >= 2:
            duration = parsed[-2]["end"] - parsed[-2]["start"]
        else:
            duration = timedelta(hours=1)
        parsed[-1]["end"] = parsed[-1]["start"] + duration

    return parsed


def compute_charge_modes(
    prices_arr, margin, charging_time_minutes, discharging_time_minutes
):
    """Compute charge/discharge/standby mode for every price slot.

    Args:
        prices_arr: List of compact rate dicts with 'from' (ISO string) and 'cost' (float)
                    — typically from the linked Electricity Price Levels sensor attributes.
        margin: Minimum cost difference between peak and valley to justify a cycle.
        charging_time_minutes: How long a full charge takes (minutes).
        discharging_time_minutes: Expected discharge duration (minutes).

    Returns:
        List of dicts with 'start', 'end', 'mode', and 'cost' keys.
    """
    parsed = _parse_compact_rates(prices_arr)
    if not parsed:
        return []

    charge_entries = [
        {"start": e["start"], "end": e["end"], "mode": "standby", "cost": e["cost"]}
        for e in parsed
    ]

    # Slide 12-hour windows from midnight of the first entry.
    first_start = charge_entries[0]["start"].replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    last_end = charge_entries[-1]["end"]
    window = timedelta(hours=12)
    window_start = first_start

    while window_start < last_end:
        _find_local_peaks(
            charge_entries,
            window_start,
            window_start + window,
            margin,
            discharging_time_minutes,
        )
        window_start += window

    _find_local_valleys(charge_entries, margin, charging_time_minutes)
    _extend_peaks(charge_entries)

    return charge_entries


# ---------------------------------------------------------------------------
# Sensor entity
# ---------------------------------------------------------------------------


class BatteryChargeModeSensor(SensorEntity):
    """Sensor that reports whether the battery should charge, discharge, or stand by."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"charge_entries"})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        source_sensor: ElectricityPriceLevelsSensor,
    ) -> None:
        self._entry = entry
        self._source_sensor = source_sensor
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

        # Battery parameters from config (or fallback defaults).
        capacity_kwh = entry.options.get(CONF_BATTERY_CAPACITY_KWH)
        max_power_w = entry.options.get(CONF_BATTERY_MAX_CHARGE_POWER_W)
        self._battery_soc_entity = entry.options.get(CONF_BATTERY_SOC_ENTITY)
        self._battery_capacity_kwh = capacity_kwh if capacity_kwh is not None else None
        self._charge_power_kw: float | None = None
        self._discharge_power_kw: float | None = None
        degradation_cost = entry.options.get(CONF_BATTERY_DEGRADATION_COST)
        self._margin = (
            degradation_cost if degradation_cost is not None else _DEFAULT_MARGIN
        )

        if capacity_kwh is not None and max_power_w is not None:
            max_power_kw = max_power_w / 1000.0
            self._charge_power_kw = max_power_kw
            # Charging time: capacity / power × 60 (minutes)
            self._charging_time_minutes = round(capacity_kwh / max_power_kw * 60)
            # Discharging time: typically 1.5× charging time (lower average discharge power)
            self._discharging_time_minutes = round(self._charging_time_minutes * 1.5)
            self._discharge_power_kw = capacity_kwh / (
                self._discharging_time_minutes / 60
            )
        else:
            self._charging_time_minutes = _DEFAULT_CHARGING_TIME_MINUTES
            self._discharging_time_minutes = _DEFAULT_DISCHARGING_TIME_MINUTES

        self._mode = "standby"
        self._planned_charge_entries: list[dict] = []
        self._charge_entries: list[dict] = []
        self._cached_attributes: dict = {}
        self._reserved_kwh = 0.0
        self._required_load_kwh = 0.0
        self._last_rates_hash: int | None = None
        self._task: asyncio.Task | None = None
        self._remove_source_listener = None
        self._waiting_for_first_value = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_source_listener = self._source_sensor.async_add_update_listener(
            self._handle_source_update
        )
        self.async_on_remove(self._remove_source_listener)
        if self._battery_soc_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._battery_soc_entity],
                    self._handle_constraint_source_update,
                )
            )
        if self._source_sensor.has_rates:
            await self._start_sensor()

    def _handle_source_update(self) -> None:
        if self.hass is None:
            return
        if self._waiting_for_first_value:
            if self._source_sensor.has_rates:
                self.hass.async_create_task(self._start_sensor())
            return
        self.hass.async_create_task(self._refresh_from_source())

    @callback
    def _handle_constraint_source_update(self, _event=None) -> None:
        """Refresh the live battery decision when SoC changes."""
        if self.hass is None or self._waiting_for_first_value:
            return
        self.hass.async_create_task(self._refresh_from_source())

    async def _start_sensor(self) -> None:
        if not self._waiting_for_first_value:
            return
        self._waiting_for_first_value = False
        await self._refresh_from_source()
        self._task = self.hass.async_create_background_task(
            self._periodic_update(), "batterychargemode_periodic_update"
        )

    async def _refresh_from_source(self) -> int:
        """Refresh the mode and attributes from the linked price sensor."""
        old_mode = self._mode
        old_attrs = self._cached_attributes
        next_update = self._recompute()
        if self._mode != old_mode or self._cached_attributes != old_attrs:
            self.async_write_ha_state()
        return next_update

    async def async_will_remove_from_hass(self) -> None:
        if self._task:
            self._task.cancel()
        await super().async_will_remove_from_hass()

    def _read_entity_float_state(self, entity_id: str | None) -> float | None:
        """Return a finite float state for the configured entity, if available."""
        if not entity_id or self.hass is None:
            return None

        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None

        try:
            value = float(str(state.state).replace(",", "."))
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Battery constraint state for %s is not numeric: %s",
                entity_id,
                state.state,
            )
            return None

        if not math.isfinite(value):
            return None
        return value

    def _battery_soc_percent(self) -> float | None:
        """Return the configured battery SoC, clamped to 0-100 percent."""
        value = self._read_entity_float_state(self._battery_soc_entity)
        if value is None:
            return None
        return max(0.0, min(100.0, value))

    def _remaining_slot_hours(self, entry: dict, now: datetime | None = None) -> float:
        """Return the remaining hours for a slot from now onward."""
        effective_start = entry["start"]
        if now is not None and entry["start"] < now:
            effective_start = now
        remaining_seconds = max(0.0, (entry["end"] - effective_start).total_seconds())
        return remaining_seconds / 3600.0

    def _apply_threshold_constraint(
        self, entries: list[dict], now: datetime, soc_percent: float
    ) -> None:
        """Block only impossible current-slot discharge when SoC exists alone."""
        current_index, current_entry = self._find_current_entry(now, entries)
        if current_index is None or current_entry is None:
            return

        if (
            current_entry["mode"] == "discharge"
            and soc_percent <= _DEFAULT_RESERVE_FRACTION * 100
        ):
            current_entry["mode"] = "standby"
            current_entry["constraint_reason"] = _DISCHARGE_BLOCKED_RESERVE_REASON

    def _apply_battery_constraints(
        self, planned_entries: list[dict], now: datetime
    ) -> list[dict]:
        """Apply SoC-aware feasibility constraints to the price-only slot plan."""
        adjusted = [entry.copy() for entry in planned_entries]
        self._reserved_kwh = 0.0
        self._required_load_kwh = 0.0

        if not adjusted:
            return adjusted

        soc_percent = self._battery_soc_percent()
        if soc_percent is None:
            return adjusted

        if (
            self._battery_capacity_kwh is None
            or self._charge_power_kw is None
            or self._discharge_power_kw is None
        ):
            self._apply_threshold_constraint(adjusted, now, soc_percent)
            return adjusted

        reserve_kwh = self._battery_capacity_kwh * _DEFAULT_RESERVE_FRACTION
        self._reserved_kwh = round(reserve_kwh, 3)
        current_energy_kwh = max(
            0.0,
            min(
                self._battery_capacity_kwh,
                self._battery_capacity_kwh * soc_percent / 100.0,
            ),
        )
        first_actionable_index = next(
            (index for index, entry in enumerate(adjusted) if entry["end"] > now),
            None,
        )
        if first_actionable_index is None:
            return adjusted

        for index in range(first_actionable_index, len(adjusted)):
            entry = adjusted[index]
            planned_mode = entry["mode"]
            if planned_mode not in {"charge", "discharge"}:
                continue

            remaining_hours = self._remaining_slot_hours(
                entry, now if index == first_actionable_index else None
            )
            if remaining_hours <= 0:
                continue

            if planned_mode == "charge":
                stored_gain_kwh = (
                    self._charge_power_kw * remaining_hours * _DEFAULT_CHARGE_EFFICIENCY
                )
                current_energy_kwh = min(
                    self._battery_capacity_kwh,
                    current_energy_kwh + stored_gain_kwh,
                )
                continue

            usable_energy_kwh = max(0.0, current_energy_kwh - reserve_kwh)
            required_energy_kwh = (
                self._discharge_power_kw
                * remaining_hours
                / _DEFAULT_DISCHARGE_EFFICIENCY
            )
            if (
                usable_energy_kwh <= _ENERGY_EPSILON_KWH
                or usable_energy_kwh + _ENERGY_EPSILON_KWH < required_energy_kwh
            ):
                entry["mode"] = "standby"
                entry["constraint_reason"] = (
                    _DISCHARGE_BLOCKED_RESERVE_REASON
                    if usable_energy_kwh <= _ENERGY_EPSILON_KWH
                    else _DISCHARGE_BLOCKED_ENERGY_REASON
                )
                continue

            current_energy_kwh = max(
                reserve_kwh,
                current_energy_kwh - required_energy_kwh,
            )

        return adjusted

    def _recompute(self) -> int:
        """Recompute charge modes from current rates. Returns seconds until next slot."""
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        now = dt_util.now().astimezone(local_tz)
        rates = self._source_sensor.compact_rates
        if not rates:
            self._last_rates_hash = None
            self._planned_charge_entries = []
            self._charge_entries = []
            self._reserved_kwh = 0.0
            self._required_load_kwh = 0.0
            return self._update_current_mode(now)

        # Skip full recomputation if rates haven't changed.
        rates_hash = hash(tuple((r.get("from"), r.get("cost")) for r in rates))
        if rates_hash != self._last_rates_hash:
            self._last_rates_hash = rates_hash
            self._planned_charge_entries = compute_charge_modes(
                rates,
                self._margin,
                self._charging_time_minutes,
                self._discharging_time_minutes,
            )

        self._charge_entries = self._apply_battery_constraints(
            self._planned_charge_entries, now
        )
        return self._update_current_mode(now)

    def _find_current_entry(
        self, now: datetime, entries: list[dict] | None = None
    ) -> tuple[int | None, dict | None]:
        """Return the index and entry covering the current time, if any."""
        search_entries = self._charge_entries if entries is None else entries
        for index, entry in enumerate(search_entries):
            if entry["start"] <= now < entry["end"]:
                return index, entry
        return None, None

    def _next_mode_change(
        self,
        now: datetime | None,
        current_index: int | None,
        current_entry: dict | None,
    ) -> datetime | None:
        """Return when the current mode block is expected to change."""
        if not self._charge_entries:
            return None

        if current_entry is None:
            if now is None:
                return None
            future_entry = next(
                (e for e in self._charge_entries if e["start"] > now), None
            )
            return future_entry["start"] if future_entry else None

        block_end = current_entry["end"]
        current_mode = current_entry["mode"]
        if current_index is None:
            current_index = self._charge_entries.index(current_entry)

        for entry in self._charge_entries[current_index + 1 :]:
            if entry["mode"] != current_mode:
                return entry["start"]
            block_end = entry["end"]

        if current_mode != "standby":
            return block_end
        return None

    def _reason_for_current_mode(
        self,
        now: datetime | None,
        current_entry: dict | None,
    ) -> str:
        """Return a human-readable explanation for the chosen mode."""
        if not self._charge_entries:
            return _WAITING_FOR_RATES_REASON

        if current_entry is None:
            if now is not None and now >= self._charge_entries[-1]["end"]:
                return _OUTSIDE_HORIZON_REASON
            return _BETWEEN_WINDOWS_REASON

        if current_entry.get("constraint_reason"):
            return current_entry["constraint_reason"]
        if current_entry["mode"] == "charge":
            return _CHARGE_REASON
        if current_entry["mode"] == "discharge":
            return _DISCHARGE_REASON

        if any(entry["mode"] != "standby" for entry in self._charge_entries):
            return _BETWEEN_WINDOWS_REASON
        return _NO_PROFITABLE_CYCLE_REASON

    def _update_current_mode(self, now: datetime | None = None) -> int:
        """Update _mode from charge_entries for the current time.

        Returns seconds until the current slot ends (for scheduling the next update).
        """
        if not self._charge_entries:
            self._mode = "standby"
            self._rebuild_cached_attributes(now)
            return 60

        if now is None:
            local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
            now = dt_util.now().astimezone(local_tz)
        current_index, current = self._find_current_entry(now)

        if current:
            self._mode = current["mode"]
            self._rebuild_cached_attributes(now, current_index, current)
            seconds_left = max(1, int((current["end"] - now).total_seconds()))
            return seconds_left + 1  # +1 to safely land inside the next slot.

        self._mode = "standby"
        self._rebuild_cached_attributes(now)
        return 60

    async def _periodic_update(self) -> None:
        while True:
            try:
                next_update = await self._refresh_from_source()
            except Exception:
                _LOGGER.exception("Error in battery charge mode periodic update")
                next_update = 60
            await asyncio.sleep(max(_MIN_SLEEP_SECONDS, next_update))

    @property
    def state(self) -> str:
        return self._mode

    @property
    def icon(self) -> str:
        if self._mode == "charge":
            return "mdi:battery-charging"
        if self._mode == "discharge":
            return "mdi:battery-arrow-down-outline"
        return "mdi:battery-outline"

    def _rebuild_cached_attributes(
        self,
        now: datetime | None = None,
        current_index: int | None = None,
        current_entry: dict | None = None,
    ) -> None:
        """Pre-build sensor attributes, including current-mode explainability."""
        if now is None and self.hass is not None:
            local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
            now = dt_util.now().astimezone(local_tz)
        if current_entry is None and now is not None:
            current_index, current_entry = self._find_current_entry(now)

        next_mode_change = self._next_mode_change(now, current_index, current_entry)
        self._cached_attributes = {
            "charge_entries": [
                {
                    "from": _format_compact_local_datetime(e["start"]),
                    "mode": e["mode"],
                    "cost": e["cost"],
                }
                for e in self._charge_entries
            ],
            "margin": self._margin,
            "charging_time_minutes": self._charging_time_minutes,
            "discharging_time_minutes": self._discharging_time_minutes,
            "reason": self._reason_for_current_mode(now, current_entry),
            "next_mode_change": (
                _format_compact_local_datetime(next_mode_change)
                if next_mode_change is not None
                else None
            ),
            "reserved_kwh": round(self._reserved_kwh, 3),
            "required_load_kwh": round(self._required_load_kwh, 3),
            "charge_source": "grid" if self._mode == "charge" else None,
        }

    @property
    def extra_state_attributes(self) -> dict:
        return self._cached_attributes
