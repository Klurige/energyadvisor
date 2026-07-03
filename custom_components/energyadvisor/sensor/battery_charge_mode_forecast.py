"""Battery charge mode forecast helpers.

Inputs:
    - Refined solar forecast rows with 15-minute boundaries.
    - Current time, battery size, reserve fraction, and learned base-load data.
Outputs:
    - Solar window boundaries, dominance checks, slot solar lookups, battery
      floor values, and restored recorder states for the planner.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

_DEFAULT_OVERNIGHT_HOURS = 7.0  # fallback overnight duration when no tomorrow forecast
_FORECAST_SLOT_MINUTES = 15
_MIN_USEFUL_SOLAR_KW = 0.05
_MIN_SOLAR_DOMINANT_KWH = 1.0  # Daily solar threshold to select solar-aware strategy.


def _solar_window_by_date(
    solar_entries: list[dict],
    tz_hint=None,
) -> dict:
    """Build per-day sunrise/sunset windows from solar forecast rows.

    Inputs:
        - solar_entries: forecast rows with `end` and `pow`.
        - tz_hint: timezone to attach to naive timestamps.
    Outputs:
        - Mapping of date -> (first useful slot start, last useful slot end).
    """
    windows: dict = {}
    for entry in solar_entries:
        end_str = entry.get("end")
        if not end_str:
            continue
        try:
            entry_end = datetime.fromisoformat(end_str)
            if entry_end.tzinfo is None and tz_hint is not None:
                entry_end = entry_end.replace(tzinfo=tz_hint)
        except ValueError:
            continue
        if entry.get("pow", 0.0) <= _MIN_USEFUL_SOLAR_KW:
            continue
        entry_start = entry_end - timedelta(minutes=_FORECAST_SLOT_MINUTES)
        d = entry_start.date()
        if d not in windows:
            windows[d] = (entry_start, entry_end)
        else:
            windows[d] = (windows[d][0], entry_end)
    return windows


def _is_solar_dominant(solar_entries: list[dict]) -> bool:
    """Return whether today's solar forecast is large enough for solar-aware mode.

    Inputs:
        - solar_entries: 15-minute forecast rows with `pow`.
    Outputs:
        - True when the useful solar energy meets the dominance threshold.
    """
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
    """Return the solar forecast kW that matches a specific schedule slot.

    Inputs:
        - solar_entries: forecast rows with `end` and `pow`.
        - slot_start / slot_end: slot boundaries to match.
    Outputs:
        - Forecast solar power in kW for the slot, or 0.0 when no match exists.
    """
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
    """Find today's and tomorrow's useful solar window boundaries.

    Inputs:
        - solar_entries: 15-minute forecast rows with `end` and `pow`.
        - now: current aware datetime used to split today from tomorrow.
    Outputs:
        - (solar_start_today, solar_end_today, solar_start_tomorrow).
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
    """Compute the dynamic battery floor and required overnight load.

    Inputs:
        - solar_entries: refined solar forecast rows.
        - now: current aware datetime.
        - base_load_kw: learned household base load in kW.
        - battery_capacity_kwh: usable battery capacity.
        - reserve_fraction: hard reserve fraction to keep untouched.
    Outputs:
        - (floor_kwh, required_load_kwh) for the current moment.
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


def _last_float_state_at_or_before(states: list, cutoff: datetime) -> float | None:
    """Return the last numeric recorder value at or before a cutoff time.

    Inputs:
        - states: recorder state rows in chronological order.
        - cutoff: latest acceptable timestamp.
    Outputs:
        - The parsed float value, or None when no numeric state exists.
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
