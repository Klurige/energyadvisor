"""Battery charge mode strategy helpers.

Inputs:
    - Compact rate rows from Energy Advisor and optional solar forecast rows.
    - Margin and timing parameters from the battery planner.
Outputs:
    - Planned charge entries, sell windows, and helper transformations used by
      the battery charge mode sensor.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from .battery_charge_mode_forecast import _solar_window_by_date

_LOGGER = logging.getLogger(__name__)


_SUMMER_SELL_WINDOWS = ((0, 10 * 60), (17 * 60, 24 * 60))
_SUMMER_SELL_SLOTS_PER_DAY = 6


def _find_local_peaks(
    charge_entries, range_start, range_end, margin, total_peak_time_minutes
):
    """Mark the most expensive slots in a window as discharge.

    Inputs:
        - charge_entries: mutable schedule rows with `start`, `end`, and `cost`.
        - range_start / range_end: time window to inspect.
        - margin: minimum price gap required to justify discharging.
        - total_peak_time_minutes: target discharge duration around the peak.
    Outputs:
        - Mutates `charge_entries` in place by marking discharge slots.
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
    """Mark the cheapest slots before each discharge block as charge.

    Inputs:
        - charge_entries: mutable schedule rows with `start`, `end`, and `cost`.
        - margin: retained planner margin passed through for symmetry.
        - min_valley_time_minutes: charging duration to cover before discharge.
    Outputs:
        - Mutates `charge_entries` in place by marking charge slots.
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
    """Extend discharge regions to cover head, tail, and inter-block gaps.

    Inputs:
        - charge_entries: mutable schedule rows with `start`, `end`, and `mode`.
    Outputs:
        - Mutates `charge_entries` in place by expanding discharge coverage.
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
    """Return whether a charge slot exists before the current entry.

    Inputs:
        - charge_entries: ordered schedule rows.
        - entry_index: current row index to inspect.
    Outputs:
        - True when an earlier charge slot exists.
    """
    return any(entry["mode"] == "charge" for entry in charge_entries[:entry_index])


def _future_charge_cost(charge_entries: list[dict], entry_index: int) -> float | None:
    """Return the cheapest later charge-slot cost, if one exists.

    Inputs:
        - charge_entries: ordered schedule rows.
        - entry_index: current row index to inspect.
    Outputs:
        - Cheapest future charge cost, or None if no later charge slot exists.
    """
    future_costs = [
        entry["cost"]
        for entry in charge_entries[entry_index + 1 :]
        if entry["mode"] == "charge"
    ]
    if not future_costs:
        return None
    return min(future_costs)


def _slot_hours(entry: dict) -> float:
    """Return the full duration of a schedule entry in hours.

    Inputs:
        - entry: schedule row with `start` and `end` datetimes.
    Outputs:
        - Slot duration in hours, never negative.
    """
    return max(0.0, (entry["end"] - entry["start"]).total_seconds() / 3600.0)


def _is_summer_sell_candidate(start: datetime) -> bool:
    """Return whether a slot start belongs to the fixed summer sell windows.

    Inputs:
        - start: slot start datetime.
    Outputs:
        - True when the slot falls inside a summer sell window.
    """
    start_minutes = start.hour * 60 + start.minute
    return any(
        window_start <= start_minutes < window_end
        for window_start, window_end in _SUMMER_SELL_WINDOWS
    )


def _entry_sell_value(entry: dict) -> float:
    """Return the numeric value used to rank summer sell slots.

    Inputs:
        - entry: schedule row with optional `credit` and `cost` fields.
    Outputs:
        - A finite value suitable for sorting sell candidates.
    """
    credit = entry.get("credit")
    if isinstance(credit, (int, float)) and math.isfinite(credit):
        return float(credit)

    cost = entry.get("cost", 0.0)
    if isinstance(cost, (int, float)) and math.isfinite(cost):
        return float(cost)
    return 0.0


def _slot_sell_score(entry: dict) -> tuple[float, float]:
    """Return the sort key used to rank summer sell candidates.

    Inputs:
        - entry: schedule row with optional `credit` and `cost` fields.
    Outputs:
        - A `(credit, cost)` tuple used for deterministic candidate ranking.
    """
    return (_entry_sell_value(entry), entry.get("cost", 0.0))


def _apply_summer_sell_strategy(
    charge_entries: list[dict],
    sellable_kwh: float | None = None,
    discharge_power_kw: float | None = None,
    solar_entries: list[dict] | None = None,
    margin: float = 0.0,
) -> None:
    """Assign discharge, maxuse, and sell modes for the solar-aware strategy.

    Inputs:
        - charge_entries: mutable schedule rows with `start`, `end`, `cost`,
          and `credit`.
        - sellable_kwh / discharge_power_kw: optional sizing inputs for dynamic
          sell-window width.
        - solar_entries: refined solar forecast rows used to detect sunset.
        - margin: battery wear margin used to choose between export and storage.
    Outputs:
        - Mutates `charge_entries` in place by assigning solar-aware modes.
    """
    has_solar_data = bool(solar_entries)
    tz_hint = charge_entries[0]["start"].tzinfo if charge_entries else None
    solar_windows = _solar_window_by_date(solar_entries or [], tz_hint=tz_hint)

    # Group entries into period 1 and period 2 per day.
    day_period1: dict = {}
    day_period2: dict = {}
    for entry in charge_entries:
        start = entry["start"]
        d = start.date()
        hour_frac = start.hour + start.minute / 60.0
        if hour_frac < 12.0:
            day_period1.setdefault(d, []).append(entry)
        else:
            day_period2.setdefault(d, []).append(entry)

    # Period 1: maxuse by default; discharge at price peaks.
    # A slot is a "peak" when its cost exceeds the minimum cost of all
    # subsequent period-1 slots on the same day by more than the margin.
    for p1_entries in day_period1.values():
        # Build suffix minimum: min cost of entries AFTER position i.
        n = len(p1_entries)
        suffix_min: list[float] = [math.inf] * n
        for i in range(n - 2, -1, -1):
            suffix_min[i] = min(
                p1_entries[i + 1].get("cost") or 0.0,
                suffix_min[i + 1],
            )
        for i, entry in enumerate(p1_entries):
            cost = entry.get("cost") or 0.0
            entry["mode"] = "discharge" if cost > suffix_min[i] + margin else "maxuse"

    # Period 2: maxuse until sunset, discharge after (sell candidates).
    for d, p2_entries in day_period2.items():
        _, sunset = solar_windows.get(d, (None, None))
        for entry in p2_entries:
            start = entry["start"]
            hour_frac = start.hour + start.minute / 60.0
            if has_solar_data:
                entry["mode"] = (
                    "maxuse" if (sunset is None or start < sunset) else "discharge"
                )
            else:
                entry["mode"] = "maxuse" if hour_frac < 17.0 else "discharge"

    # Sell selection (period 2 only): peak-and-expand per day.
    for d in set(day_period1.keys()) | set(day_period2.keys()):
        p2_entries = day_period2.get(d, [])
        if not p2_entries:
            continue

        # Dynamic: all period-2 slots are candidates (sell peak can precede sunset).
        # Fallback: only the discharge slots (17:00+).
        if has_solar_data:
            candidates = list(p2_entries)
        else:
            candidates = [e for e in p2_entries if e["mode"] == "discharge"]
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

        # Seed: highest-ranked slot; ties broken by earliest time.
        peak_idx = max(
            range(len(candidates)),
            key=lambda i: (*_slot_sell_score(candidates[i]), -i),
        )
        candidates[peak_idx]["mode"] = "sell"
        left = peak_idx
        right = peak_idx

        # Expand outward one slot at a time, picking the higher adjacent candidate.
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

    Inputs:
        - charge_entries: mutable schedule rows with peak/valley markers.
        - margin: battery wear margin used when comparing future charge costs.
    Outputs:
        - Mutates `charge_entries` in place by refining output-mode rows.
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


def _parse_compact_rates(rates: list[dict]) -> list[dict]:
    """Parse compact rate dicts into datetime-based schedule rows.

    Inputs:
        - rates: compact rows with `from`, `cost`, and `credit`.
    Outputs:
        - Parsed schedule rows with `start`, `end`, `cost`, and `credit`.
    """
    if not rates:
        return []

    local_tz = dt_util.get_default_time_zone()
    parsed = []
    for r in rates:
        from_str = r.get("from")
        if not from_str:
            continue
        try:
            start = datetime.fromisoformat(from_str).replace(tzinfo=local_tz)
            cost = float(r.get("cost", 0.0))
            credit = float(r.get("credit", 0.0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(cost) or not math.isfinite(credit):
            continue
        parsed.append({"start": start, "cost": cost, "credit": credit})

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


def _apply_price_arbitrage_strategy(
    charge_entries: list[dict],
    margin: float,
    charging_time_minutes: int,
    discharging_time_minutes: int,
) -> None:
    """Apply price-based charge/discharge scheduling for low-solar days.

    Inputs:
        - charge_entries: mutable schedule rows with `start`, `end`, and `cost`.
        - margin: minimum price spread needed to justify cycling.
        - charging_time_minutes / discharging_time_minutes: planner sizing
          inputs used to estimate how wide the charge and discharge windows
          should be.
    Outputs:
        - Mutates `charge_entries` in place by assigning price-arbitrage modes.
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
