"""Forecast helpers for the Battery Charge Mode sensor."""

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
    """Return {date: (sunrise, sunset)} from solar forecast entries.

    sunrise: start of first 15-min slot with power above the useful threshold.
    sunset:  end of last such slot (start of that slot + _FORECAST_SLOT_MINUTES).
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
