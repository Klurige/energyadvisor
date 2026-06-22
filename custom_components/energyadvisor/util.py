"""Shared helpers for the Energy Advisor integration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

LEVEL_TO_COMPACT = {
    "Low": "L",
    "Medium": "M",
    "High": "H",
    "Unknown": "U",
}


def level_to_compact(level: str | None) -> str:
    """Translate a human-readable level into its compact representation."""
    return LEVEL_TO_COMPACT.get(level or "", "U")


def infer_level_length_minutes(rates: Sequence[Mapping[str, Any]]) -> int:
    """Infer the native slot length from the first valid rate entry."""
    for rate in rates:
        start = rate.get("start")
        end = rate.get("end")
        if isinstance(start, datetime) and isinstance(end, datetime) and end > start:
            return max(1, int((end - start).total_seconds() / 60))
    return 0


def _chunk_to_level(chunk: Sequence[str]) -> str:
    """Collapse minute-level data into one compact level."""
    if not chunk or any(level == "U" for level in chunk):
        return "U"
    if all(level == "L" for level in chunk):
        return "L"
    if all(level in {"L", "M"} for level in chunk):
        return "M"
    return "H"


def build_levels_payload_from_rates(
    rates: Sequence[Mapping[str, Any]],
    low_threshold: float | None,
    high_threshold: float | None,
    reference_time: datetime,
    requested_length: int = 0,
    fill_unknown: bool = False,
) -> dict[str, int | float | str | None]:
    """Build a compact two-day level string from internal rate data."""
    level_length = requested_length or infer_level_length_minutes(rates)
    if level_length <= 0:
        return {
            "level_length": 0,
            "levels": "",
            "low_threshold": low_threshold,
            "high_threshold": high_threshold,
        }

    day_start = reference_time.replace(hour=0, minute=0, second=0, microsecond=0)
    range_end = day_start + timedelta(days=2)
    total_minutes = max(0, int((range_end - day_start).total_seconds() / 60))
    minute_levels = ["U"] * total_minutes

    for rate in sorted(rates, key=lambda item: item["start"]):
        start = rate.get("start")
        end = rate.get("end")
        if (
            not isinstance(start, datetime)
            or not isinstance(end, datetime)
            or end <= start
        ):
            continue

        overlap_start = max(start, day_start)
        overlap_end = min(end, range_end)
        if overlap_end <= overlap_start:
            continue

        start_index = max(0, int((overlap_start - day_start).total_seconds() // 60))
        end_index = min(
            total_minutes, int((overlap_end - day_start).total_seconds() // 60)
        )
        if end_index <= start_index:
            continue

        minute_levels[start_index:end_index] = [level_to_compact(rate.get("level"))] * (
            end_index - start_index
        )

    levels = "".join(
        _chunk_to_level(minute_levels[index : index + level_length])
        for index in range(0, total_minutes, level_length)
    )
    if not fill_unknown:
        levels = levels.rstrip("U")

    return {
        "level_length": level_length,
        "levels": levels,
        "low_threshold": low_threshold,
        "high_threshold": high_threshold,
    }
