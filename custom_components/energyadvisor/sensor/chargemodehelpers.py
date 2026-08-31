from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

def find_current_mode(modes: list[dict], reference_time: datetime | None = None) -> dict:
    now = reference_time or dt_util.now()
    previous_slot = None
    for slot in modes:
        slot_from = dt_util.parse_datetime(slot.get("from"))
        if slot_from is None:
            _LOGGER.warning(f"Skipping mode slot with invalid from value: {slot}")
            continue
        if slot_from.tzinfo is None:
            slot_from = slot_from.replace(tzinfo=now.tzinfo)
        else:
            slot_from = dt_util.as_local(slot_from)
        if slot_from <= now:
            previous_slot = slot
        else:
            break
    if previous_slot:
        return previous_slot
    else:
        return default_modes(now)[0]

def default_modes(reference_time: datetime | None = None):
    # Default mode is maxuse if no price data is available.
    base_time = reference_time or dt_util.now()
    today_midnight = base_time.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%dT%H:%M")
    return [
        {
            "from": today_midnight,
            "mode": "maxuse",
            "target_soc": None,
            "cost": 0.0,
            "credit": 0.0,
        }
    ]

def find_peaks_in_modes(modes: list[dict], margin: float) -> list[dict]:
    # Find any peaks in modes by looking at cost. A peak is defined as a mode that has a higher cost than the previous and next modes,
    # and separated by at least one entry that is at least margin lower than the peak.
    # It is safe to assume that the first and last entries are not peaks.
    if len(modes) < 3:
        return []
    peaks = []
    peak_indexes = []
    by_cost_indexes = sorted(range(len(modes)), key=lambda idx: -modes[idx].get("cost"))
    for mode_index in by_cost_indexes:
        mode = modes[mode_index]
        mode_cost = mode.get("cost")
        previous_mode = modes[mode_index - 1] if mode_index > 0 else None
        next_mode = modes[mode_index + 1] if mode_index < len(modes) - 1 else None

        if previous_mode is None or next_mode is None:
            continue
        if previous_mode.get("cost") < mode_cost and next_mode.get("cost") < mode_cost:
            # Check if there is at least one entry before and after the peak that is at least margin lower than the peak.
            has_lower_before = any(
                modes[i].get("cost") <= mode_cost - margin for i in range(mode_index)
            )
            has_lower_after = any(
                modes[i].get("cost") <= mode_cost - margin for i in range(mode_index + 1, len(modes))
            )
            if has_lower_before and has_lower_after:
                peak_indexes.append(mode_index)
    if peak_indexes:
        # There should be a local min, lower by margin, between each peak.
        # Start with the lowest peak by cost and remove it if the local min requirements are not met.
        filtered_peak_indexes = sorted(peak_indexes)
        for peak_index in sorted(peak_indexes, key=lambda idx: modes[idx].get("cost")):
            if peak_index not in filtered_peak_indexes:
                continue
            peak_position = filtered_peak_indexes.index(peak_index)
            previous_peak_index = (
                filtered_peak_indexes[peak_position - 1] if peak_position > 0 else 0
            )
            next_peak_index = (
                filtered_peak_indexes[peak_position + 1]
                if peak_position < len(filtered_peak_indexes) - 1
                else len(modes) - 1
            )
            min_before = min(modes[j].get("cost") for j in range(previous_peak_index, peak_index))
            min_after = min(
                modes[j].get("cost") for j in range(peak_index + 1, next_peak_index + 1)
            )
            if not (
                min_before <= modes[peak_index].get("cost") - margin
                and min_after <= modes[peak_index].get("cost") - margin
            ):
                filtered_peak_indexes.remove(peak_index)
        peaks = [modes[i] for i in filtered_peak_indexes]

    return peaks