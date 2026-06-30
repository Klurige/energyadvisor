"""Battery Charge Mode sensor for Home Assistant.

Determines whether a home battery should be in standby, charging, maxuse,
discharge, or sell mode based on the electricity price schedule from the
ElectricityPriceLevels sensor.
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
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_DEGRADATION_COST,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_MAX_DISCHARGE_POWER_W,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CENTRAL_HEATING_ACTIVE_ENTITY,
    CONF_EXCLUDE_FROM_RECORDING,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_POWER_ENTITY,
    CONF_POWER_METER_CONSUMPTION,
    CONF_WATER_HEATER_ACTIVE_ENTITY,
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
_DEFAULT_OVERNIGHT_HOURS = 7.0  # fallback overnight duration when no tomorrow forecast
_DEFAULT_CHARGE_EFFICIENCY = 0.95
_DEFAULT_DISCHARGE_EFFICIENCY = 0.95
_ENERGY_EPSILON_KWH = 0.05
_FORECAST_SLOT_MINUTES = 15
_MIN_USEFUL_SOLAR_KW = 0.05
_MIN_SOLAR_DOMINANT_KWH = 1.0  # Daily solar threshold to select solar-aware strategy.
_SUMMER_SELL_WINDOWS = ((0, 10 * 60), (17 * 60, 24 * 60))
_SUMMER_SELL_SLOTS_PER_DAY = 6

# Base-load learning: quiet-night window is 01:00–04:00.
_LEARNING_WINDOW_START_HOUR = 1
_LEARNING_WINDOW_END_HOUR = 4
_LEARNING_WINDOW_HOURS = float(_LEARNING_WINDOW_END_HOUR - _LEARNING_WINDOW_START_HOUR)
_MAX_LEARNING_HISTORY = 30  # Rolling average over the last 30 valid nights.

_STORAGE_KEY = "energyadvisor_battery_base_load"
_STORAGE_VERSION = 1

# Dawn SoC feedback: self-calibrating sell safety margin.
# The battery has a hardware 5% cutoff, so dawn SoC can never be below reserve.
# Feedback is therefore asymmetric: decrease margin when dawn SoC is well above
# reserve (sold too little); increase margin slightly when dawn SoC is at or near
# reserve (battery may have hit the hardware floor before dawn, risking grid import).
_DAWN_MARGIN_HIGH_THRESHOLD_PCT = 15.0  # dawn SoC above (reserve + this) → decrease
_DAWN_MARGIN_LOW_THRESHOLD_PCT = (
    5.0  # dawn SoC within this % of reserve → small increase
)
_DAWN_MARGIN_DECREASE_STEP_KWH = 0.5  # kWh removed per well-stocked dawn
_DAWN_MARGIN_INCREASE_STEP_KWH = 0.2  # kWh added per tight dawn (small safety buffer)
_DAWN_MARGIN_MAX_KWH = 8.0  # absolute ceiling on the sell safety margin

_WAITING_FOR_RATES_REASON = "Waiting for electricity price data."
_OUTSIDE_HORIZON_REASON = (
    "Current time is outside the available battery schedule horizon."
)
_BETWEEN_WINDOWS_REASON = (
    "Current slot is outside the scheduled battery activity windows."
)
_NO_PROFITABLE_CYCLE_REASON = (
    "No profitable battery cycle is scheduled in the available price horizon."
)
_CHARGE_REASON = (
    "Charging is scheduled in a low-price window ahead of higher-price periods."
)
_MAXUSE_REASON = "Maximizing self-consumption is scheduled because this slot is outside the selected summer sell slots."
_DISCHARGE_REASON = "Discharging is scheduled because using the battery now beats importing electricity now."
_SELL_REASON = "Selling is scheduled because this slot is one of the six highest-valued periods between 00:00-10:00 and 17:00-24:00."
_BATTERY_OUTPUT_BLOCKED_RESERVE_REASON = (
    "Battery output is blocked because the battery is already at its reserve floor."
)
_BATTERY_OUTPUT_BLOCKED_ENERGY_REASON = "Battery output is blocked because the usable battery energy above reserve cannot cover the remaining slot."
_SELL_FLOOR_BLOCKED_REASON = (
    "Sell downgraded to maxuse — not enough energy to reach the next solar window."
)
_NO_BASE_LOAD_REASON = (
    "Waiting for the first quiet night (01:00–04:00 with water heater and central heating "
    "both off) to learn household base load. Battery floor is not enforced yet."
)
_BATTERY_OUTPUT_MODES = frozenset({"maxuse", "discharge", "sell"})


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
            entry["mode_source"] = "peak"


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
            entry["mode_source"] = "charge"


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
            entry["mode_source"] = "extension_head"

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
            entry["mode_source"] = "extension_tail"

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
                    entry["mode_source"] = "extension_gap"


def _has_prior_charge(charge_entries: list[dict], entry_index: int) -> bool:
    """Return whether a charge slot exists before the current entry."""
    return any(entry["mode"] == "charge" for entry in charge_entries[:entry_index])


def _future_charge_cost(charge_entries: list[dict], entry_index: int) -> float | None:
    """Return the cheapest later charge slot cost, if one exists."""
    future_costs = [
        entry["cost"]
        for entry in charge_entries[entry_index + 1 :]
        if entry["mode"] == "charge"
    ]
    if not future_costs:
        return None
    return min(future_costs)


def _slot_hours(entry: dict) -> float:
    """Return the full duration of a schedule entry in hours."""
    return max(0.0, (entry["end"] - entry["start"]).total_seconds() / 3600.0)


def _is_summer_sell_candidate(start: datetime) -> bool:
    """Return whether a slot start belongs to the fixed summer sell windows."""
    start_minutes = start.hour * 60 + start.minute
    return any(
        window_start <= start_minutes < window_end
        for window_start, window_end in _SUMMER_SELL_WINDOWS
    )


def _entry_sell_value(entry: dict) -> float:
    """Return the value used to rank summer sell slots."""
    credit = entry.get("credit")
    if isinstance(credit, (int, float)) and math.isfinite(credit):
        return float(credit)

    cost = entry.get("cost", 0.0)
    if isinstance(cost, (int, float)) and math.isfinite(cost):
        return float(cost)
    return 0.0


def _slot_sell_score(entry: dict) -> tuple[float, float]:
    """Compound sort key for ranking summer sell candidates.

    Primary: export credit (or 0 when no credit is provided).
    Secondary: raw electricity cost, used as an avoidance-value proxy when
    credit values are equal or absent (e.g. in test fixtures without credits).
    Ties are therefore broken by which slot has the highest spot price.
    """
    return (_entry_sell_value(entry), entry.get("cost", 0.0))


def _has_solar_surplus_in_rate_slot(
    solar_entries: list[dict],
    slot_start: datetime,
    slot_end: datetime,
    base_load_kw: float,
) -> bool:
    """Return True if any 15-min sub-slot within [slot_start, slot_end) has solar > base load."""
    step = timedelta(minutes=_FORECAST_SLOT_MINUTES)
    t = slot_start
    while t < slot_end:
        if _solar_kw_for_slot(solar_entries, t, t + step) > base_load_kw:
            return True
        t += step
    return False


def _apply_summer_sell_strategy(
    charge_entries: list[dict],
    sellable_kwh: float | None = None,
    discharge_power_kw: float | None = None,
    solar_entries: list[dict] | None = None,
    base_load_kw: float | None = None,
    margin: float = 0.0,
) -> None:
    """Assign discharge/maxuse/sell modes for the solar-dominant (summer) strategy.

    Initial mode assignment (per slot, before sell selection):
    - When solar forecast is available, a slot is **maxuse** when solar surplus
      exists (solar_kw > base_load) AND the export credit is below the battery
      wear margin (not worth exporting — better to store for the evening sell).
      All other slots are **discharge**: the battery covers load and any solar
      surplus is exported to the grid at the prevailing credit price.
    - When no solar forecast is available, fall back to fixed windows:
      sell-candidate hours (00:00–10:00, 17:00–24:00) → discharge;
      mid-day (10:00–17:00) → maxuse.

    Sell selection (peak-and-expand):
    All discharge slots are sell candidates.  The highest-valued slot is chosen
    as the peak, then the window expands outward — always picking the higher
    adjacent candidate — until the sell target (derived from sellable_kwh /
    discharge_power) is reached.  This produces a near-contiguous sell block
    that minimises inverter warm-up transitions.
    """
    has_solar_data = bool(solar_entries)
    base_kw = base_load_kw or 0.0

    entries_by_day: dict = {}
    for entry in charge_entries:
        if has_solar_data:
            surplus = _has_solar_surplus_in_rate_slot(
                solar_entries, entry["start"], entry["end"], base_kw
            )
            credit = entry.get("credit") or 0.0
            # Store in battery when solar is available and export is not worth
            # more than battery wear cost; otherwise export to grid.
            if surplus and credit <= margin:
                entry["mode"] = "maxuse"
            else:
                entry["mode"] = "discharge"
        else:
            # No solar forecast: fall back to fixed sell-candidate windows.
            if _is_summer_sell_candidate(entry["start"]):
                entry["mode"] = "discharge"
            else:
                entry["mode"] = "maxuse"
        entries_by_day.setdefault(entry["start"].date(), []).append(entry)

    for day_entries in entries_by_day.values():
        # All discharge slots are sell candidates — sell windows are now
        # determined dynamically by the discharge/maxuse assignment above.
        candidates = [e for e in day_entries if e["mode"] == "discharge"]
        if not candidates:
            continue

        candidates.sort(key=lambda entry: entry["start"])

        # Derive per-slot energy and target sell count.
        if sellable_kwh is not None and discharge_power_kw is not None:
            slot_secs = (candidates[0]["end"] - candidates[0]["start"]).total_seconds()
            slot_hours = slot_secs / 3600.0
            energy_per_slot = discharge_power_kw * slot_hours
            if energy_per_slot > 0 and sellable_kwh > 0:
                default_target = math.ceil(sellable_kwh / energy_per_slot)
            else:
                default_target = 0
        else:
            default_target = _SUMMER_SELL_SLOTS_PER_DAY

        target = min(default_target, len(candidates))
        if target <= 0:
            continue

        # Seed: highest-ranked slot; ties (same credit and cost) broken by
        # earliest time.
        peak_idx = max(
            range(len(candidates)),
            key=lambda i: (*_slot_sell_score(candidates[i]), -i),
        )
        candidates[peak_idx]["mode"] = "sell"
        left = peak_idx
        right = peak_idx

        # Expand outward one slot at a time, always picking the higher-ranked
        # adjacent candidate (left or right in time order).
        _EMPTY_SCORE: tuple[float, float] = (-math.inf, -math.inf)
        while (right - left + 1) < target:
            left_score = (
                _slot_sell_score(candidates[left - 1]) if left > 0 else _EMPTY_SCORE
            )
            right_score = (
                _slot_sell_score(candidates[right + 1])
                if right < len(candidates) - 1
                else _EMPTY_SCORE
            )
            if left_score == _EMPTY_SCORE and right_score == _EMPTY_SCORE:
                break
            if left_score >= right_score:
                left -= 1
                candidates[left]["mode"] = "sell"
            else:
                right += 1
                candidates[right]["mode"] = "sell"


def _classify_output_modes(charge_entries: list[dict], margin: float) -> None:
    """Classify discharge-like slots into maxuse, discharge, or sell.

    The price-window planner first marks cheap charge periods and expensive
    battery-output periods. Step 6 refines the battery-output periods into:

    - maxuse: self-consume battery energy when no earlier charge slot exists
    - discharge: use the battery for house load after earlier cheap charging
    - sell: export battery energy when a later cheaper charge window exists
      and the current export credit is high enough to justify buying back later
    """
    for index, entry in enumerate(charge_entries):
        if entry["mode"] != "discharge":
            continue

        mode_source = entry.get("mode_source", "peak")
        has_prior_charge = _has_prior_charge(charge_entries, index)

        if isinstance(mode_source, str) and mode_source.startswith("extension"):
            entry["mode"] = "discharge" if has_prior_charge else "maxuse"
            continue

        future_charge_cost = _future_charge_cost(charge_entries, index)
        if (
            future_charge_cost is not None
            and entry.get("credit", 0.0) >= future_charge_cost + margin
        ):
            entry["mode"] = "sell"
        elif has_prior_charge:
            entry["mode"] = "discharge"
        else:
            entry["mode"] = "maxuse"


def _is_solar_dominant(solar_entries: list[dict]) -> bool:
    """Return True when today's solar forecast exceeds the awareness threshold."""
    if not solar_entries:
        return False
    total_kwh = sum(
        e.get("pow", 0.0) * (_FORECAST_SLOT_MINUTES / 60.0)
        for e in solar_entries
        if e.get("pow", 0.0) > _MIN_USEFUL_SOLAR_KW
    )
    return total_kwh >= _MIN_SOLAR_DOMINANT_KWH


def _solar_kw_for_slot(
    solar_entries: list[dict], slot_start: datetime, slot_end: datetime
) -> float:
    """Return the forecast solar power (kW) for the 15-min slot [slot_start, slot_end)."""
    slot_tz = slot_end.tzinfo
    for entry in solar_entries:
        end_str = entry.get("end")
        if not end_str:
            continue
        try:
            entry_end = datetime.fromisoformat(end_str)
            if entry_end.tzinfo is None and slot_tz is not None:
                entry_end = entry_end.replace(tzinfo=slot_tz)
        except ValueError:
            continue
        entry_start = entry_end - timedelta(minutes=_FORECAST_SLOT_MINUTES)
        if abs((entry_start - slot_start).total_seconds()) < 60:
            return max(0.0, entry.get("pow", 0.0))
    return 0.0


def _find_solar_window(
    solar_entries: list[dict], now: datetime
) -> tuple[datetime | None, datetime | None, datetime | None]:
    """Return (solar_start_today, solar_end_today, solar_start_tomorrow).

    solar_start_today:    start of the first useful solar slot for today's calendar day
                          (may be in the past if we are already in the solar window)
    solar_end_today:      end of the last useful solar slot for today's calendar day
    solar_start_tomorrow: start of the first useful solar slot for tomorrow

    The overnight gap the battery must cover is:
        solar_end_today → solar_start_tomorrow
    """
    today = now.date()
    tomorrow = today + timedelta(days=1)
    local_tz = now.tzinfo
    solar_start_today: datetime | None = None
    solar_end_today: datetime | None = None
    solar_start_tomorrow: datetime | None = None
    for entry in solar_entries:
        end_str = entry.get("end")
        if not end_str:
            continue
        try:
            entry_end = datetime.fromisoformat(end_str)
            if entry_end.tzinfo is None and local_tz is not None:
                entry_end = entry_end.replace(tzinfo=local_tz)
        except ValueError:
            continue
        entry_start = entry_end - timedelta(minutes=_FORECAST_SLOT_MINUTES)
        if entry.get("pow", 0.0) <= _MIN_USEFUL_SOLAR_KW:
            continue
        if entry_start.date() == today:
            if solar_start_today is None:
                solar_start_today = entry_start
            solar_end_today = entry_end  # keep updating to capture the last useful slot
        elif entry_start.date() == tomorrow and solar_start_tomorrow is None:
            solar_start_tomorrow = entry_start
    return solar_start_today, solar_end_today, solar_start_tomorrow


def _compute_floor_kwh(
    solar_entries: list[dict],
    now: datetime,
    base_load_kw: float,
    battery_capacity_kwh: float,
    reserve_fraction: float,
) -> tuple[float, float]:
    """Return (floor_kwh, required_load_kwh) for the current moment.

    floor_kwh = reserve + overnight_load + daytime_deficit

    During daytime (solar producing), overnight_load is the energy needed for
    the upcoming darkness window (today's last solar slot through tomorrow's
    first solar slot). During nighttime, overnight_load decreases linearly as
    we approach the next sunrise.
    """
    reserve_kwh = battery_capacity_kwh * reserve_fraction
    solar_start_today, solar_end_today, solar_start_tomorrow = _find_solar_window(
        solar_entries, now
    )

    in_daytime = (
        solar_start_today is not None
        and solar_end_today is not None
        and solar_start_today <= now < solar_end_today
    )

    if in_daytime:
        # Reserve battery for tonight: solar_end_today → solar_start_tomorrow
        if solar_start_tomorrow is not None:
            overnight_hours = max(
                0.0,
                (solar_start_tomorrow - solar_end_today).total_seconds() / 3600.0,
            )
        else:
            # No tomorrow forecast yet; use a conservative default
            overnight_hours = _DEFAULT_OVERNIGHT_HOURS
        nighttime_load_kwh = base_load_kw * overnight_hours

        # Daytime deficit: remaining solar today may not cover remaining house load
        remaining_solar_kwh = 0.0
        for entry in solar_entries:
            end_str = entry.get("end")
            if not end_str:
                continue
            try:
                entry_end = datetime.fromisoformat(end_str)
                if entry_end.tzinfo is None and now.tzinfo is not None:
                    entry_end = entry_end.replace(tzinfo=now.tzinfo)
            except ValueError:
                continue
            if entry_end <= now:
                continue
            entry_start = entry_end - timedelta(minutes=_FORECAST_SLOT_MINUTES)
            if entry_start.date() != now.date():
                continue
            if entry.get("pow", 0.0) > _MIN_USEFUL_SOLAR_KW:
                remaining_solar_kwh += entry["pow"] * (_FORECAST_SLOT_MINUTES / 60.0)

        remaining_daylight_hours = max(
            0.0, (solar_end_today - now).total_seconds() / 3600.0
        )
        daytime_deficit_kwh = max(
            0.0, base_load_kw * remaining_daylight_hours - remaining_solar_kwh
        )
    else:
        # Nighttime: protect the battery until the next sunrise
        if solar_start_today is not None and now < solar_start_today:
            # Pre-dawn: sunrise is still today
            next_sunrise = solar_start_today
        elif solar_start_tomorrow is not None:
            # Post-sunset: sunrise is tomorrow
            next_sunrise = solar_start_tomorrow
        else:
            next_sunrise = None

        nighttime_load_kwh = (
            base_load_kw * max(0.0, (next_sunrise - now).total_seconds() / 3600.0)
            if next_sunrise is not None
            else 0.0
        )
        daytime_deficit_kwh = 0.0

    required_load_kwh = nighttime_load_kwh + daytime_deficit_kwh
    floor_kwh = reserve_kwh + required_load_kwh
    return min(floor_kwh, battery_capacity_kwh), required_load_kwh


def _apply_price_arbitrage_strategy(
    charge_entries: list[dict],
    margin: float,
    charging_time_minutes: int,
    discharging_time_minutes: int,
) -> None:
    """Apply price-based charge/discharge scheduling for low-solar days.

    Re-uses the existing peak/valley/extend/classify pipeline that was the
    original battery algorithm. On days with minimal solar the battery is
    treated as a pure price-arbitrage asset: charge cheap, sell/discharge
    at expensive peaks.
    """
    if not charge_entries:
        return
    range_start = charge_entries[0]["start"]
    range_end = charge_entries[-1]["end"]
    _find_local_peaks(
        charge_entries, range_start, range_end, margin, discharging_time_minutes
    )
    _find_local_valleys(charge_entries, margin, charging_time_minutes)
    _extend_peaks(charge_entries)
    _classify_output_modes(charge_entries, margin)


def _parse_compact_rates(rates: list[dict]) -> list[dict]:
    """Parse compact rate dicts (from attributes) into datetime-based dicts.

    Compact format: {"from": "2026-05-25T00:00", "cost": 1.234, "credit": 0.987, ...}
    Output format:  {"start": datetime, "end": datetime, "cost": float, "credit": float}
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
        parsed.append(
            {
                "start": start,
                "cost": r.get("cost", 0.0),
                "credit": r.get("credit", 0.0),
            }
        )

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


def _last_float_state_at_or_before(states: list, cutoff: datetime) -> float | None:
    """Return the float value of the last state recorded at or before *cutoff*.

    States are assumed to be in chronological order (as returned by the recorder).
    Returns None if no suitable state is found or the state value is not numeric.
    """
    best = None
    for state in states:
        if state.last_updated <= cutoff:
            best = state
        else:
            break
    if best is None:
        return None
    try:
        value = float(str(best.state).replace(",", "."))
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def compute_charge_modes(
    prices_arr,
    margin,
    charging_time_minutes,
    discharging_time_minutes,
    solar_entries=None,
    sellable_kwh=None,
    discharge_power_kw=None,
    base_load_kw=None,
):
    """Compute battery modes for every price slot.

    Selects between two strategies based on the daily solar forecast:

    - **Solar-aware** (expected solar ≥ threshold): slots are discharge by
      default (solar surplus exported at the prevailing credit price), maxuse
      when solar surplus is available but the export credit is below the
      battery wear margin (better to store for the evening sell), and sell
      for the peak-and-expand window.  When `sellable_kwh` and
      `discharge_power_kw` are provided, the sell-window width is derived
      dynamically from the battery's available capacity above the floor.
    - **Price-arbitrage** (minimal solar): find cheap charge windows and
      expensive discharge/sell windows using the price-spread algorithm.

    Args:
        prices_arr: List of compact rate dicts with 'from', 'cost', 'credit'.
        margin: Minimum cost spread / battery wear cost per kWh.  Used both
                to gate charge-discharge cycles (price-arbitrage) and as the
                export-credit threshold for discharge vs maxuse decisions
                (solar-aware strategy).
        charging_time_minutes: Full-charge duration in minutes.
        discharging_time_minutes: Expected discharge duration in minutes.
        solar_entries: Optional refined solar forecast (15-min slots from the
                       solar coordinator). Used to select the strategy and to
                       determine per-slot solar surplus.
        sellable_kwh: Battery energy available to sell above the floor (kWh).
                      Used to set the sell width dynamically when solar-dominant.
        discharge_power_kw: Inverter discharge power (kW). Used with
                            `sellable_kwh` to calculate the target slot count.
        base_load_kw: Household base load (kW). Used with `solar_entries` to
                      determine whether a slot has a solar surplus worth storing.
    Returns:
        List of dicts with 'start', 'end', 'mode', 'cost', 'credit'.
    """
    parsed = _parse_compact_rates(prices_arr)
    if not parsed:
        return []

    charge_entries = [
        {
            "start": e["start"],
            "end": e["end"],
            "mode": "standby",
            "cost": e["cost"],
            "credit": e.get("credit", 0.0),
        }
        for e in parsed
    ]

    if _is_solar_dominant(solar_entries or []):
        _apply_summer_sell_strategy(
            charge_entries,
            sellable_kwh=sellable_kwh,
            discharge_power_kw=discharge_power_kw,
            solar_entries=solar_entries,
            base_load_kw=base_load_kw,
            margin=margin,
        )
    else:
        _apply_price_arbitrage_strategy(
            charge_entries, margin, charging_time_minutes, discharging_time_minutes
        )

    return charge_entries


# ---------------------------------------------------------------------------
# Sensor entity
# ---------------------------------------------------------------------------


class BatteryChargeModeSensor(SensorEntity):
    """Sensor that reports the current Energy Advisor battery mode."""

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
        max_discharge_power_w = entry.options.get(CONF_BATTERY_MAX_DISCHARGE_POWER_W)
        self._battery_soc_entity = entry.options.get(CONF_BATTERY_SOC_ENTITY)
        self._battery_capacity_kwh = capacity_kwh if capacity_kwh is not None else None
        self._charge_power_kw: float | None = None
        self._discharge_power_kw: float | None = None
        self._household_base_load_kw: float | None = None
        degradation_cost = entry.options.get(CONF_BATTERY_DEGRADATION_COST)
        self._margin = (
            degradation_cost if degradation_cost is not None else _DEFAULT_MARGIN
        )

        # Entities for base-load learning.
        self._power_entity: str | None = entry.options.get(CONF_POWER_ENTITY)
        self._power_meter_entity: str | None = entry.options.get(
            CONF_POWER_METER_CONSUMPTION
        )
        self._grid_import_entity: str | None = entry.options.get(
            CONF_GRID_IMPORT_ENTITY
        )
        self._grid_export_entity: str | None = entry.options.get(
            CONF_GRID_EXPORT_ENTITY
        )
        self._water_heater_active_entity: str | None = entry.options.get(
            CONF_WATER_HEATER_ACTIVE_ENTITY
        )
        self._central_heating_active_entity: str | None = entry.options.get(
            CONF_CENTRAL_HEATING_ACTIVE_ENTITY
        )

        # Learning state (populated from storage and updated nightly).
        self._base_load_history: list[float] = []
        self._meter_reading_at_01: float | None = None
        self._learning_window_active: bool = False
        self._quiet_night: bool = False
        self._store: Store | None = None

        # Dawn SoC feedback: sell safety margin adjusted each morning.
        self._sell_safety_margin_kwh: float = 0.0
        self._was_in_daytime: bool = False  # detect night→day transition
        self._dawn_feedback_date: object = None  # prevent double-trigger per day

        if capacity_kwh is not None and max_power_w is not None:
            max_power_kw = max_power_w / 1000.0
            self._charge_power_kw = max_power_kw
            # Charging time: capacity / charge_power (minutes)
            self._charging_time_minutes = round(capacity_kwh / max_power_kw * 60)
            # Discharge power: use configured value when available; otherwise
            # assume same as charge power (symmetric inverter).
            if max_discharge_power_w is not None:
                discharge_kw = max_discharge_power_w / 1000.0
            else:
                discharge_kw = max_power_kw
            self._discharge_power_kw = discharge_kw
            self._discharging_time_minutes = round(capacity_kwh / discharge_kw * 60)
        else:
            self._charging_time_minutes = _DEFAULT_CHARGING_TIME_MINUTES
            self._discharging_time_minutes = _DEFAULT_DISCHARGING_TIME_MINUTES

        self._mode = "standby"
        self._planned_charge_entries: list[dict] = []
        self._charge_entries: list[dict] = []
        self._cached_attributes: dict = {}
        self._reserved_kwh = 0.0
        self._required_load_kwh = 0.0
        self._solar_dominant: bool = False
        self._last_plan_inputs_hash: int | None = None
        self._task: asyncio.Task | None = None
        self._remove_source_listener = None
        self._waiting_for_first_value = True
        self._update_listeners: list = []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_source_listener = self._source_sensor.async_add_update_listener(
            self._handle_source_update
        )
        self.async_on_remove(self._remove_source_listener)
        solar_coordinator = self._solar_coordinator()
        if solar_coordinator is not None:
            solar_coordinator.register_update_callback(
                self._handle_solar_forecast_update
            )
            self.async_on_remove(
                lambda: solar_coordinator.unregister_update_callback(
                    self._handle_solar_forecast_update
                )
            )
        if self._battery_soc_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._battery_soc_entity],
                    self._handle_constraint_source_update,
                )
            )

        # Base-load learning: snapshot meter at 01:00, evaluate at 04:00.
        await self._load_learned_data()
        if self._power_entity or self._power_meter_entity:
            self.async_on_remove(
                async_track_time_change(
                    self.hass,
                    self._async_on_learning_window_start,
                    hour=_LEARNING_WINDOW_START_HOUR,
                    minute=0,
                    second=0,
                )
            )
            self.async_on_remove(
                async_track_time_change(
                    self.hass,
                    self._async_on_learning_window_end,
                    hour=_LEARNING_WINDOW_END_HOUR,
                    minute=0,
                    second=0,
                )
            )
        big_consumers = [
            e
            for e in [
                self._water_heater_active_entity,
                self._central_heating_active_entity,
            ]
            if e
        ]
        if big_consumers:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    big_consumers,
                    self._handle_big_consumer_change,
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
    def _handle_solar_forecast_update(self) -> None:
        """Refresh the plan when the linked solar forecast changes."""
        if self.hass is None or self._waiting_for_first_value:
            return
        self.hass.async_create_task(self._refresh_from_source())

    @callback
    def _handle_constraint_source_update(self, _event=None) -> None:
        """Refresh the live battery decision when SoC changes."""
        if self.hass is None or self._waiting_for_first_value:
            return
        self.hass.async_create_task(self._refresh_from_source())

    # ------------------------------------------------------------------
    # Base-load learning
    # ------------------------------------------------------------------

    async def _load_learned_data(self) -> None:
        """Restore the base-load rolling average from HA storage."""
        self._store = Store(self.hass, _STORAGE_VERSION, _STORAGE_KEY)
        data = await self._store.async_load()
        if data and isinstance(data.get("base_load_history"), list):
            self._base_load_history = [
                float(v)
                for v in data["base_load_history"]
                if isinstance(v, (int, float))
                and math.isfinite(float(v))
                and float(v) > 0
            ]
            if self._base_load_history:
                self._household_base_load_kw = sum(self._base_load_history) / len(
                    self._base_load_history
                )
                _LOGGER.debug(
                    "Base load restored: %.3f kW from %d nights.",
                    self._household_base_load_kw,
                    len(self._base_load_history),
                )

        if isinstance(data, dict):
            margin = data.get("sell_safety_margin_kwh")
            if isinstance(margin, (int, float)) and math.isfinite(float(margin)):
                self._sell_safety_margin_kwh = max(0.0, float(margin))
                _LOGGER.debug(
                    "Sell safety margin restored: %.3f kWh.",
                    self._sell_safety_margin_kwh,
                )

        if not self._base_load_history:
            await self._bootstrap_base_load_from_history()

    async def _bootstrap_base_load_from_history(self) -> None:
        """Pre-populate base-load history from the recorder on first run.

        Tries the power-entity approach first (inverter/house power W, averaged
        per night with outlier filtering), then falls back to the energy-meter
        diff approach.  Runs only once; subsequent nights are added nightly.
        """
        if self._power_entity:
            await self._bootstrap_from_power_entity()
        elif self._power_meter_entity:
            await self._bootstrap_from_energy_meter()

    async def _bootstrap_from_power_entity(self) -> None:
        """Bootstrap base load by averaging power_entity recorder history per night."""
        try:
            from homeassistant.helpers.recorder import get_instance
        except ImportError:
            return

        recorder = get_instance(self.hass)
        if recorder is None:
            return

        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        now_local = dt_util.now().astimezone(local_tz)
        today = now_local.date()
        bootstrapped: list[float] = []

        start_days_ago = 0 if now_local.hour >= _LEARNING_WINDOW_END_HOUR else 1

        for days_ago in range(start_days_ago, _MAX_LEARNING_HISTORY + 1):
            night = today - timedelta(days=days_ago)
            window_start = datetime(
                night.year,
                night.month,
                night.day,
                _LEARNING_WINDOW_START_HOUR,
                0,
                0,
                tzinfo=local_tz,
            )
            window_end = datetime(
                night.year,
                night.month,
                night.day,
                _LEARNING_WINDOW_END_HOUR,
                0,
                0,
                tzinfo=local_tz,
            )
            base_load_kw = await self._average_power_kw_from_recorder(
                window_start, window_end
            )
            if base_load_kw is None:
                _LOGGER.debug(
                    "Bootstrap (power): skipping %s — no valid average.", night
                )
                continue
            _LOGGER.debug(
                "Bootstrap (power): %s  base load %.3f kW.", night, base_load_kw
            )
            bootstrapped.append(base_load_kw)
            if len(bootstrapped) >= _MAX_LEARNING_HISTORY:
                break

        if not bootstrapped:
            _LOGGER.debug(
                "Bootstrap (power): no valid nights found in recorder history."
            )
            return

        self._base_load_history = list(reversed(bootstrapped))
        self._household_base_load_kw = sum(self._base_load_history) / len(
            self._base_load_history
        )
        await self._save_learned_data()
        _LOGGER.info(
            "Base load bootstrapped from %d nights of recorder history: %.3f kW.",
            len(self._base_load_history),
            self._household_base_load_kw,
        )

    async def _bootstrap_from_energy_meter(self) -> None:
        """Bootstrap base load from energy-meter (grid import) kWh diff per night.

        Only valid when the house has no battery or solar — i.e. when grid import
        equals total consumption during the quiet window.  Checks big-consumer
        activity sensors and compares meter readings at window start/end.
        """
        try:
            from homeassistant.helpers.recorder import get_instance
            from homeassistant.components.recorder import history as recorder_history
        except ImportError:
            return

        recorder = get_instance(self.hass)
        if recorder is None:
            return

        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        now_local = dt_util.now().astimezone(local_tz)
        today = now_local.date()
        bootstrapped: list[float] = []

        start_days_ago = 0 if now_local.hour >= _LEARNING_WINDOW_END_HOUR else 1

        for days_ago in range(start_days_ago, _MAX_LEARNING_HISTORY + 1):
            night = today - timedelta(days=days_ago)
            window_start = datetime(
                night.year,
                night.month,
                night.day,
                _LEARNING_WINDOW_START_HOUR,
                0,
                0,
                tzinfo=local_tz,
            )
            window_end = datetime(
                night.year,
                night.month,
                night.day,
                _LEARNING_WINDOW_END_HOUR,
                0,
                0,
                tzinfo=local_tz,
            )

            # Check big consumers were off the whole window.
            quiet = True
            for consumer in [
                self._water_heater_active_entity,
                self._central_heating_active_entity,
            ]:
                if not consumer:
                    continue
                consumer_states = await recorder.async_add_executor_job(
                    recorder_history.state_changes_during_period,
                    self.hass,
                    window_start,
                    window_end,
                    consumer,
                )
                if any(
                    s.state in ("on", "true", "1")
                    for s in consumer_states.get(consumer, [])
                ):
                    quiet = False
                    break
            if not quiet:
                _LOGGER.debug(
                    "Bootstrap (meter): skipping %s — big consumer was active.", night
                )
                continue

            # Query meter with a small buffer so we capture the reading
            # that was active at the start/end of the window.
            meter_states = await recorder.async_add_executor_job(
                recorder_history.state_changes_during_period,
                self.hass,
                window_start - timedelta(minutes=10),
                window_end + timedelta(minutes=10),
                self._power_meter_entity,
            )
            states = meter_states.get(self._power_meter_entity, [])

            reading_start = _last_float_state_at_or_before(
                states, window_start + timedelta(minutes=5)
            )
            reading_end = _last_float_state_at_or_before(
                states, window_end + timedelta(minutes=5)
            )
            if reading_start is None or reading_end is None:
                _LOGGER.debug(
                    "Bootstrap (meter): skipping %s — no meter data (start=%s, end=%s, %d states).",
                    night,
                    reading_start,
                    reading_end,
                    len(states),
                )
                continue

            diff_kwh = reading_end - reading_start
            if not (0 < diff_kwh < 5.0):  # sanity: max ~1.67 kW average
                _LOGGER.debug(
                    "Bootstrap (meter): skipping %s — diff %.3f kWh outside sanity band.",
                    night,
                    diff_kwh,
                )
                continue

            _LOGGER.debug(
                "Bootstrap (meter): %s  base load %.3f kW (diff %.3f kWh).",
                night,
                diff_kwh / _LEARNING_WINDOW_HOURS,
                diff_kwh,
            )
            bootstrapped.append(diff_kwh / _LEARNING_WINDOW_HOURS)
            if len(bootstrapped) >= _MAX_LEARNING_HISTORY:
                break

        if not bootstrapped:
            _LOGGER.debug(
                "Bootstrap (meter): no quiet nights found in recorder history."
            )
            return

        self._base_load_history = list(reversed(bootstrapped))
        self._household_base_load_kw = sum(self._base_load_history) / len(
            self._base_load_history
        )
        await self._save_learned_data()
        _LOGGER.info(
            "Base load bootstrapped from %d nights of recorder history: %.3f kW.",
            len(self._base_load_history),
            self._household_base_load_kw,
        )

    async def _save_learned_data(self) -> None:
        """Persist the base-load rolling average to HA storage."""
        if self._store is None:
            return
        await self._store.async_save(
            {
                "base_load_history": self._base_load_history,
                "sell_safety_margin_kwh": round(self._sell_safety_margin_kwh, 3),
            }
        )

    async def _average_power_kw_from_recorder(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> float | None:
        """Compute average household power draw (kW) over the window.

        household_kW = trimmed_avg(power_entity W)/1000 + avg(grid_import kW) - avg(grid_export kW)

        power_entity is the inverter AC output (solar + battery net), in Watts.
        grid_import and grid_export are the net grid flows, in kW.
        Readings of power_entity above 2.5× the median are dropped as big-consumer
        spikes before averaging.  Returns None if fewer than 6 valid readings exist,
        more than half are filtered, or the result falls outside 50 W – 3000 W.
        """
        if not self._power_entity:
            return None
        try:
            from homeassistant.helpers.recorder import get_instance
            from homeassistant.components.recorder import history as recorder_history
        except ImportError:
            return None

        rec = get_instance(self.hass)
        if rec is None:
            return None

        # --- Inverter power (W): outlier-filtered trimmed mean ---
        states_dict = await rec.async_add_executor_job(
            recorder_history.state_changes_during_period,
            self.hass,
            window_start,
            window_end,
            self._power_entity,
        )
        values: list[float] = []
        for s in states_dict.get(self._power_entity, []):
            try:
                v = float(s.state)
                if v >= 0:
                    values.append(v)
            except (TypeError, ValueError):
                pass

        if len(values) < 6:
            _LOGGER.debug(
                "Power averaging: only %d valid readings in window — not enough data.",
                len(values),
            )
            return None

        sorted_vals = sorted(values)
        median = sorted_vals[len(sorted_vals) // 2]
        lo = median / 3.0  # drop-out / brief-disconnect floor
        hi = median * 2.5  # big-consumer spike ceiling

        filtered = [v for v in values if lo <= v <= hi]
        if len(filtered) < len(values) // 2:
            _LOGGER.debug(
                "Power averaging: %d/%d readings outside [%.0f W, %.0f W] — noisy data, skipping.",
                len(values) - len(filtered),
                len(values),
                lo,
                hi,
            )
            return None

        avg_inverter_kw = sum(filtered) / len(filtered) / 1000.0

        # --- Grid import/export correction (kW) ---
        # house_use = inverter_output + grid_import - grid_export
        grid_correction_kw = 0.0
        for entity, sign in [
            (self._grid_import_entity, +1.0),
            (self._grid_export_entity, -1.0),
        ]:
            if not entity:
                continue
            states_dict = await rec.async_add_executor_job(
                recorder_history.state_changes_during_period,
                self.hass,
                window_start,
                window_end,
                entity,
            )
            grid_vals: list[float] = []
            for s in states_dict.get(entity, []):
                try:
                    v = float(s.state)
                    if v >= 0:
                        grid_vals.append(v)
                except (TypeError, ValueError):
                    pass
            if grid_vals:
                grid_correction_kw += sign * (sum(grid_vals) / len(grid_vals))

        result_kw = avg_inverter_kw + grid_correction_kw
        if not (0.05 < result_kw < 3.0):
            _LOGGER.debug(
                "Power averaging: result %.3f kW outside sanity band.", result_kw
            )
            return None

        return result_kw

    @callback
    def _async_on_learning_window_start(self, now: datetime) -> None:
        """At 01:00: open the quiet window (snapshot energy meter if using diff approach)."""
        if self._power_entity:
            # Power-averaging approach: nothing to snapshot; recorder query happens at 04:00.
            self._learning_window_active = True
            self._quiet_night = True
        elif self._power_meter_entity:
            reading = self._read_entity_float_state(self._power_meter_entity)
            if reading is None:
                self._learning_window_active = False
                return
            self._meter_reading_at_01 = reading
            self._learning_window_active = True
            self._quiet_night = True
        else:
            self._learning_window_active = False

    @callback
    def _handle_big_consumer_change(self, event) -> None:
        """Invalidate the quiet window if a big consumer turns on during it."""
        if not self._learning_window_active:
            return
        new_state = event.data.get("new_state")
        if new_state is not None and new_state.state in ("on", "true", "1"):
            self._quiet_night = False

    async def _async_on_learning_window_end(self, now: datetime) -> None:
        """At 04:00: update base-load rolling average if the window was quiet."""
        if not self._learning_window_active:
            self._learning_window_active = False
            return

        self._learning_window_active = False

        if not self._quiet_night:
            _LOGGER.debug(
                "Base load learning: skipping — big consumer was active during 01:00–04:00."
            )
            return

        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        today_local = dt_util.now().astimezone(local_tz).date()

        if self._power_entity:
            # Power-averaging approach: query recorder for the just-closed window.
            window_start = datetime(
                today_local.year,
                today_local.month,
                today_local.day,
                _LEARNING_WINDOW_START_HOUR,
                0,
                0,
                tzinfo=local_tz,
            )
            window_end = datetime(
                today_local.year,
                today_local.month,
                today_local.day,
                _LEARNING_WINDOW_END_HOUR,
                0,
                0,
                tzinfo=local_tz,
            )
            base_load_kw = await self._average_power_kw_from_recorder(
                window_start, window_end
            )
            if base_load_kw is None:
                _LOGGER.debug(
                    "Base load learning: power averaging yielded no valid result."
                )
                return
        elif self._power_meter_entity:
            if self._meter_reading_at_01 is None:
                return
            reading_04 = self._read_entity_float_state(self._power_meter_entity)
            if reading_04 is None:
                return
            diff_kwh = reading_04 - self._meter_reading_at_01
            if diff_kwh <= 0:
                _LOGGER.debug(
                    "Base load learning: non-positive diff (%.3f kWh), skipping.",
                    diff_kwh,
                )
                return
            base_load_kw = diff_kwh / _LEARNING_WINDOW_HOURS
        else:
            return

        self._base_load_history.append(base_load_kw)
        if len(self._base_load_history) > _MAX_LEARNING_HISTORY:
            self._base_load_history.pop(0)

        self._household_base_load_kw = sum(self._base_load_history) / len(
            self._base_load_history
        )
        _LOGGER.info(
            "Base load learning: updated to %.3f kW (%d nights in average).",
            self._household_base_load_kw,
            len(self._base_load_history),
        )
        await self._save_learned_data()

    async def _update_sell_margin_at_dawn(self) -> None:
        """Adjust sell safety margin based on dawn SoC reading.

        Called once per day when solar first starts (night→day transition).
        The battery has a 5% hardware cutoff, so dawn SoC is always ≥ reserve.

        - Dawn SoC well above reserve → sold too conservatively → decrease margin.
        - Dawn SoC at or near reserve → battery may have hit the hardware floor
          before dawn, risking overnight grid imports → small increase for buffer.
        """
        if self._battery_capacity_kwh is None:
            return
        soc_pct = self._battery_soc_percent()
        if soc_pct is None:
            return

        capacity_kwh = self._battery_capacity_kwh
        reserve_pct = _DEFAULT_RESERVE_FRACTION * 100.0
        above_reserve_pct = soc_pct - reserve_pct  # how far above 5%

        if above_reserve_pct > _DAWN_MARGIN_HIGH_THRESHOLD_PCT:
            # Battery significantly above reserve at dawn → sell strategy was
            # too conservative; reduce the margin to allow more evening selling.
            self._sell_safety_margin_kwh = max(
                0.0, self._sell_safety_margin_kwh - _DAWN_MARGIN_DECREASE_STEP_KWH
            )
            _LOGGER.info(
                "Dawn SoC feedback: SoC %.1f%% is %.1f%% above reserve — "
                "sell was conservative. Margin decreased to %.2f kWh.",
                soc_pct,
                above_reserve_pct,
                self._sell_safety_margin_kwh,
            )
        elif above_reserve_pct <= _DAWN_MARGIN_LOW_THRESHOLD_PCT:
            # Battery near reserve — hardware floor may have been hit before dawn.
            # Add a small buffer to protect against early overnight grid imports.
            self._sell_safety_margin_kwh = min(
                _DAWN_MARGIN_MAX_KWH,
                self._sell_safety_margin_kwh + _DAWN_MARGIN_INCREASE_STEP_KWH,
            )
            _LOGGER.info(
                "Dawn SoC feedback: SoC %.1f%% is close to reserve — "
                "adding buffer. Margin increased to %.2f kWh.",
                soc_pct,
                self._sell_safety_margin_kwh,
            )
        else:
            _LOGGER.debug(
                "Dawn SoC feedback: SoC %.1f%% (%.1f%% above reserve) — no margin change.",
                soc_pct,
                above_reserve_pct,
            )

        await self._save_learned_data()
        self._notify_update_listeners()

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

    # ------------------------------------------------------------------
    # Public accessors for diagnostic sensors
    # ------------------------------------------------------------------

    def async_add_update_listener(self, listener) -> callable:
        """Register a callback fired after every recomputation."""
        self._update_listeners.append(listener)

        def _remove():
            if listener in self._update_listeners:
                self._update_listeners.remove(listener)

        return _remove

    def _notify_update_listeners(self) -> None:
        for listener in tuple(self._update_listeners):
            listener()

    @property
    def household_base_load_w(self) -> float | None:
        """Learned household base load in Watts, or None if not yet known."""
        if self._household_base_load_kw is None:
            return None
        return self._household_base_load_kw * 1000.0

    @property
    def learning_nights(self) -> int:
        """Number of quiet nights used in the rolling base-load average."""
        return len(self._base_load_history)

    @property
    def solar_dominant(self) -> bool:
        """True when today's solar forecast exceeds the awareness threshold."""
        return self._solar_dominant

    @property
    def battery_floor_kwh(self) -> float:
        """Energy (kWh) that must stay in the battery to cover load until solar."""
        return self._required_load_kwh

    @property
    def battery_floor_pct(self) -> float | None:
        """Floor expressed as percentage of battery capacity (0–100)."""
        if self._battery_capacity_kwh is None or self._battery_capacity_kwh <= 0:
            return None
        return round(self._required_load_kwh / self._battery_capacity_kwh * 100.0, 1)

    @property
    def sell_safety_margin_kwh(self) -> float:
        """Learned sell safety margin (kWh) added on top of the floor constraint."""
        return self._sell_safety_margin_kwh

    @property
    def battery_soc_forecast(self) -> list[dict]:
        """Forecast battery SoC% over the planned charge schedule.

        Returns two concatenated segments:
        1. Future slots (now → end of charge_entries): forward simulation from
           current actual SoC using planned modes and solar forecast.
        2. Extension (end of charge_entries → now+24h): continued simulation
           using solar forecast in maxuse mode when tomorrow's prices are not
           yet available.

        The anchor point (last completed slot boundary, up to 15 min in the
        past) is prepended so apexcharts-card always has a recent data point.

        Returns a list of {end, soc_pct, mode} dicts, one per 15-min slot.
        Returns an empty list when required inputs are not available.
        """
        if not self._charge_entries or self._battery_capacity_kwh is None:
            return []
        soc_pct = self._battery_soc_percent()
        if soc_pct is None:
            return []
        if self.hass is None:
            return []

        now = dt_util.now()
        capacity_kwh = self._battery_capacity_kwh
        base_load_kw = self._household_base_load_kw or 0.0
        charge_power_kw = self._charge_power_kw or 0.0
        discharge_power_kw = self._discharge_power_kw or base_load_kw
        solar_entries = self._solar_forecast_entries()
        current_kwh = capacity_kwh * soc_pct / 100.0

        def _delta(mode: str, slot_hours: float, solar_kw: float) -> float:
            if mode == "charge":
                return charge_power_kw * slot_hours * _DEFAULT_CHARGE_EFFICIENCY
            if mode == "sell":
                return -discharge_power_kw * slot_hours
            if mode in {"maxuse", "discharge"}:
                net_kw = solar_kw - base_load_kw
                if net_kw >= 0:
                    return net_kw * slot_hours * _DEFAULT_CHARGE_EFFICIENCY
                return net_kw * slot_hours / _DEFAULT_DISCHARGE_EFFICIENCY
            return 0.0  # standby: battery idle

        result: list[dict] = []
        last_completed_entry: dict | None = None

        # --- Forward simulation from current SoC through planned slots ---
        sim_kwh = current_kwh
        last_entry_end: datetime | None = None
        for entry in self._charge_entries:
            if entry["end"] <= now:
                last_completed_entry = entry
                continue
            slot_start = max(entry["start"], now)
            slot_hours = (entry["end"] - slot_start).total_seconds() / 3600.0
            if slot_hours <= 0:
                continue
            solar_kw = _solar_kw_for_slot(solar_entries, entry["start"], entry["end"])
            mode = entry["mode"]
            sim_kwh = max(
                0.0, min(capacity_kwh, sim_kwh + _delta(mode, slot_hours, solar_kw))
            )
            result.append(
                {
                    "end": entry["end"].isoformat(),
                    "soc_pct": round(sim_kwh / capacity_kwh * 100.0, 1),
                    "mode": mode,
                }
            )
            last_entry_end = entry["end"]

        # --- Extension: simulate forward until now+24h ---
        target_end = now + timedelta(hours=24)
        ext_start = last_entry_end if last_entry_end is not None else now
        if ext_start < target_end:
            slot_duration = timedelta(minutes=_FORECAST_SLOT_MINUTES)
            ext_kwh = sim_kwh if result else current_kwh
            t = ext_start
            while t < target_end:
                slot_end = t + slot_duration
                solar_kw = _solar_kw_for_slot(solar_entries, t, slot_end)
                slot_hours = slot_duration.total_seconds() / 3600.0
                ext_kwh = max(
                    0.0,
                    min(capacity_kwh, ext_kwh + _delta("maxuse", slot_hours, solar_kw)),
                )
                result.append(
                    {
                        "end": slot_end.isoformat(),
                        "soc_pct": round(ext_kwh / capacity_kwh * 100.0, 1),
                        "mode": "maxuse",
                    }
                )
                t = slot_end

        # Prepend anchor (last completed slot boundary) for apexcharts-card
        if last_completed_entry is not None:
            result.insert(
                0,
                {
                    "end": last_completed_entry["end"].isoformat(),
                    "soc_pct": round(current_kwh / capacity_kwh * 100.0, 1),
                    "mode": last_completed_entry.get("mode", "standby"),
                },
            )

        return result

    def _read_entity_float_state(self, entity_id: str | None) -> float | None:
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

    def _solar_coordinator(self):
        """Return the optional solar forecast coordinator for this config entry."""
        runtime_data = getattr(self._entry, "runtime_data", None)
        return getattr(runtime_data, "solar_coordinator", None)

    def _solar_forecast_entries(self) -> list[dict]:
        """Return the refined solar forecast entries, if configured."""
        solar_coordinator = self._solar_coordinator()
        if solar_coordinator is None:
            return []

        forecasts = getattr(solar_coordinator, "forecast", None)
        return forecasts if isinstance(forecasts, list) else []

    def _remaining_slot_hours(self, entry: dict, now: datetime | None = None) -> float:
        """Return the remaining hours for a slot from now onward."""
        effective_start = entry["start"]
        if now is not None and entry["start"] < now:
            effective_start = now
        remaining_seconds = max(0.0, (entry["end"] - effective_start).total_seconds())
        return remaining_seconds / 3600.0

    def _bridge_to_solar_required_energy_kwh(
        self,
        entries: list[dict],
        start_index: int,
        bridge_start: datetime,
        solar_start: datetime | None,
        load_power_kw: float | None,
    ) -> float:
        """Return battery energy needed to cover load until useful solar begins."""
        if solar_start is None or load_power_kw is None or load_power_kw <= 0:
            return 0.0
        if solar_start <= bridge_start:
            return 0.0

        required_energy_kwh = 0.0
        for index in range(start_index, len(entries)):
            entry = entries[index]
            if entry["start"] >= solar_start:
                break
            if entry["cost"] <= 0 or entry["mode"] not in {"standby", "maxuse"}:
                continue

            effective_start = max(
                entry["start"], bridge_start if index == start_index else entry["start"]
            )
            effective_end = min(entry["end"], solar_start)
            remaining_hours = max(
                0.0, (effective_end - effective_start).total_seconds() / 3600.0
            )
            if remaining_hours <= 0:
                continue

            required_energy_kwh += (
                load_power_kw * remaining_hours / _DEFAULT_DISCHARGE_EFFICIENCY
            )

        return required_energy_kwh

    def _output_power_kw_for_mode(self, mode: str) -> float | None:
        """Return the power assumption used for the given output mode."""
        if mode == "maxuse" and self._household_base_load_kw is not None:
            return self._household_base_load_kw
        return self._discharge_power_kw

    def _apply_threshold_constraint(
        self, entries: list[dict], now: datetime, soc_percent: float
    ) -> None:
        """Block only impossible current-slot battery output when SoC exists alone."""
        current_index, current_entry = self._find_current_entry(now, entries)
        if current_index is None or current_entry is None:
            return

        if (
            current_entry["mode"] in _BATTERY_OUTPUT_MODES
            and soc_percent <= _DEFAULT_RESERVE_FRACTION * 100
        ):
            current_entry["mode"] = "standby"
            current_entry["constraint_reason"] = _BATTERY_OUTPUT_BLOCKED_RESERVE_REASON

    def _apply_battery_constraints(
        self, planned_entries: list[dict], now: datetime
    ) -> list[dict]:
        """Apply SoC and battery-floor constraints to the planned schedule.

        When base load is known:
        - Compute a dynamic floor = reserve + nighttime load + daytime solar deficit.
        - If battery is below the floor, downgrade sell → maxuse (stop exporting
          but keep self-consuming) until the floor is met.
        - If battery is at or below reserve, block all output → standby.

        When base load is not yet learned:
        - Only enforce the reserve floor on the current slot.
        """
        adjusted = [entry.copy() for entry in planned_entries]
        self._reserved_kwh = 0.0
        self._required_load_kwh = 0.0

        solar_entries = self._solar_forecast_entries()
        soc_percent = self._battery_soc_percent()

        # --- Floor calculation when we have both base load and battery size ---
        if (
            self._household_base_load_kw is not None
            and self._battery_capacity_kwh is not None
        ):
            floor_kwh, self._required_load_kwh = _compute_floor_kwh(
                solar_entries,
                now,
                self._household_base_load_kw,
                self._battery_capacity_kwh,
                _DEFAULT_RESERVE_FRACTION,
            )
            self._reserved_kwh = self._battery_capacity_kwh * _DEFAULT_RESERVE_FRACTION

            # Effective floor adds the learned sell safety margin on top of the
            # calculated floor. The margin self-calibrates at each dawn: it
            # decreases when the battery was well above reserve at sunrise
            # (sell was too conservative) and increases slightly when it was at
            # or near reserve (battery may have hit the hardware 5% cutoff early).
            effective_floor_kwh = min(
                floor_kwh + self._sell_safety_margin_kwh,
                self._battery_capacity_kwh,
            )

            if soc_percent is not None:
                capacity_kwh = self._battery_capacity_kwh
                battery_kwh = capacity_kwh * soc_percent / 100.0
                reserve_kwh = capacity_kwh * _DEFAULT_RESERVE_FRACTION
                charge_power_kw = self._charge_power_kw or 0.0
                discharge_power_kw = self._discharge_power_kw or (
                    self._household_base_load_kw or 0.0
                )
                base_load_kw = self._household_base_load_kw or 0.0

                # Forward simulation: evaluate constraint based on the projected
                # battery level at each slot, not the static current level.
                # This ensures that evening sell entries remain permitted on sunny
                # days even when the battery is low in the morning — solar will
                # recharge during the day before the sell window opens.
                simulated_kwh = battery_kwh
                for entry in adjusted:
                    if entry["end"] <= now:
                        continue  # past slots: no change, no simulation step

                    slot_start = max(entry["start"], now)
                    slot_hours = (entry["end"] - slot_start).total_seconds() / 3600.0
                    if slot_hours <= 0:
                        continue

                    solar_kw = _solar_kw_for_slot(
                        solar_entries, entry["start"], entry["end"]
                    )
                    mode = entry["mode"]

                    # Apply constraint based on projected level at slot start
                    if mode in _BATTERY_OUTPUT_MODES:
                        if simulated_kwh <= reserve_kwh:
                            if mode != "maxuse":
                                # sell/discharge: block — insufficient energy.
                                entry["mode"] = "standby"
                                entry["constraint_reason"] = (
                                    _BATTERY_OUTPUT_BLOCKED_RESERVE_REASON
                                )
                                mode = "standby"
                            # maxuse: the inverter hardware enforces the 5%
                            # reserve cutoff; leave the mode so the inverter
                            # self-limits gracefully.
                        elif simulated_kwh < effective_floor_kwh and mode == "sell":
                            entry["mode"] = "maxuse"
                            entry["constraint_reason"] = _SELL_FLOOR_BLOCKED_REASON
                            mode = "maxuse"

                    # Simulate battery delta for this slot
                    if mode == "charge":
                        delta_kwh = (
                            charge_power_kw * slot_hours * _DEFAULT_CHARGE_EFFICIENCY
                        )
                    elif mode == "sell":
                        delta_kwh = -discharge_power_kw * slot_hours
                    elif mode in {"maxuse", "discharge"}:
                        # Battery covers load deficit; excess solar charges battery
                        net_kw = solar_kw - base_load_kw
                        if net_kw >= 0:
                            delta_kwh = net_kw * slot_hours * _DEFAULT_CHARGE_EFFICIENCY
                        else:
                            delta_kwh = (
                                net_kw * slot_hours / _DEFAULT_DISCHARGE_EFFICIENCY
                            )
                    else:  # standby: battery explicitly idle
                        delta_kwh = 0.0

                    # For maxuse and discharge, the hardware 5% reserve is a
                    # hard floor; clamp the simulation there so that
                    # subsequent daytime solar recharge is modelled correctly
                    # from reserve rather than from zero.
                    sim_floor = reserve_kwh if mode in {"maxuse", "discharge"} else 0.0
                    simulated_kwh = max(
                        sim_floor, min(capacity_kwh, simulated_kwh + delta_kwh)
                    )
            return adjusted

        # --- Fallback: only SoC reserve check on current slot ---
        if soc_percent is not None:
            self._apply_threshold_constraint(adjusted, now, soc_percent)

        return adjusted

    def _recompute(self) -> int:
        """Recompute charge modes from current rates. Returns seconds until next slot."""
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        now = dt_util.now().astimezone(local_tz)
        rates = self._source_sensor.compact_rates
        solar_entries = self._solar_forecast_entries()

        if not rates:
            self._last_plan_inputs_hash = None
            self._planned_charge_entries = []
            self._charge_entries = []
            self._reserved_kwh = 0.0
            self._required_load_kwh = 0.0
            return self._update_current_mode(now)

        # Include solar dominance in the hash so strategy re-selects when solar
        # flips between dominant and minimal (e.g. morning forecast update).
        solar_dominant = _is_solar_dominant(solar_entries)
        self._solar_dominant = solar_dominant

        # Compute sellable energy for dynamic sell-width in solar-dominant mode.
        # sellable = battery capacity above (floor + safety margin).
        sellable_kwh: float | None = None
        if (
            solar_dominant
            and self._battery_capacity_kwh is not None
            and self._household_base_load_kw is not None
        ):
            floor_kwh, _ = _compute_floor_kwh(
                solar_entries,
                now,
                self._household_base_load_kw,
                self._battery_capacity_kwh,
                _DEFAULT_RESERVE_FRACTION,
            )
            sellable_kwh = max(
                0.0,
                self._battery_capacity_kwh - floor_kwh - self._sell_safety_margin_kwh,
            )

        # Round to 0.1 kWh so minor floating-point drift does not force a replan.
        plan_inputs_hash = hash(
            (
                tuple((r.get("from"), r.get("cost"), r.get("credit")) for r in rates),
                solar_dominant,
                round(sellable_kwh, 1) if sellable_kwh is not None else None,
            )
        )
        if plan_inputs_hash != self._last_plan_inputs_hash:
            self._last_plan_inputs_hash = plan_inputs_hash
            self._planned_charge_entries = compute_charge_modes(
                rates,
                self._margin,
                self._charging_time_minutes,
                self._discharging_time_minutes,
                solar_entries=solar_entries,
                sellable_kwh=sellable_kwh,
                discharge_power_kw=self._discharge_power_kw,
                base_load_kw=self._household_base_load_kw,
            )

        self._charge_entries = self._apply_battery_constraints(
            self._planned_charge_entries, now
        )

        # Dawn detection: trigger sell-margin feedback on first recompute after sunrise.
        solar_start_t, solar_end_t, _ = _find_solar_window(solar_entries, now)
        in_daytime = (
            solar_start_t is not None
            and solar_end_t is not None
            and solar_start_t <= now < solar_end_t
        )
        if in_daytime and not self._was_in_daytime:
            today = now.date()
            if self._dawn_feedback_date != today:
                self._dawn_feedback_date = today
                self.hass.async_create_task(self._update_sell_margin_at_dawn())
        self._was_in_daytime = in_daytime

        result = self._update_current_mode(now)
        self._notify_update_listeners()
        return result

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
        if current_entry["mode"] == "maxuse":
            if self._household_base_load_kw is None and self._power_meter_entity:
                return _NO_BASE_LOAD_REASON
            return _MAXUSE_REASON
        if current_entry["mode"] == "discharge":
            return _DISCHARGE_REASON
        if current_entry["mode"] == "sell":
            return _SELL_REASON

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
        if self._mode == "maxuse":
            return "mdi:home-lightning-bolt-outline"
        if self._mode == "discharge":
            return "mdi:battery-arrow-down-outline"
        if self._mode == "sell":
            return "mdi:battery-arrow-up-outline"
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
            "sell_safety_margin_kwh": round(self._sell_safety_margin_kwh, 3),
            "base_load_kw": (
                round(self._household_base_load_kw, 3)
                if self._household_base_load_kw is not None
                else None
            ),
            "charge_source": "grid" if self._mode == "charge" else None,
        }

    @property
    def extra_state_attributes(self) -> dict:
        return self._cached_attributes
