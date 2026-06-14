"""Tests for the util module."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.electricitypricelevels.util import (
    build_levels_payload_from_rates,
    infer_level_length_minutes,
)

UTC = ZoneInfo("UTC")


def _rate(start: datetime, minutes: int, level: str) -> dict:
    return {
        "start": start,
        "end": start + timedelta(minutes=minutes),
        "level": level,
    }


def test_infer_level_length_minutes():
    """Test native level length detection."""
    start = datetime(2025, 8, 9, 0, 0, tzinfo=UTC)
    rates = [_rate(start, 60, "Low")]
    assert infer_level_length_minutes(rates) == 60


def test_build_levels_payload_trims_unknown_tail_when_not_requested():
    """Test trailing unknown slots are trimmed by default."""
    start = datetime(2025, 8, 9, 0, 0, tzinfo=UTC)
    rates = [
        _rate(start, 30, "Low"),
        _rate(start + timedelta(minutes=30), 30, "High"),
    ]

    result = build_levels_payload_from_rates(
        rates,
        low_threshold=1.0,
        high_threshold=2.0,
        reference_time=datetime(2025, 8, 9, 12, 0, tzinfo=UTC),
        requested_length=30,
        fill_unknown=False,
    )

    assert result["level_length"] == 30
    assert result["levels"] == "LH"


def test_build_levels_payload_fill_unknown_returns_two_days():
    """Test fill_unknown keeps the full two-day window."""
    start = datetime(2025, 8, 9, 0, 0, tzinfo=UTC)
    rates = [
        _rate(start, 30, "Low"),
        _rate(start + timedelta(minutes=30), 30, "High"),
    ]

    result = build_levels_payload_from_rates(
        rates,
        low_threshold=1.0,
        high_threshold=2.0,
        reference_time=datetime(2025, 8, 9, 12, 0, tzinfo=UTC),
        requested_length=30,
        fill_unknown=True,
    )

    assert result["levels"].startswith("LH")
    assert len(result["levels"]) == 96
    assert set(result["levels"][2:]) == {"U"}


def test_build_levels_payload_is_aligned_to_current_day():
    """Test tomorrow-only data preserves today's unknown prefix."""
    tomorrow = datetime(2025, 8, 10, 0, 0, tzinfo=UTC)
    rates = [_rate(tomorrow, 60, "Low")]

    result = build_levels_payload_from_rates(
        rates,
        low_threshold=1.0,
        high_threshold=2.0,
        reference_time=datetime(2025, 8, 9, 12, 0, tzinfo=UTC),
        requested_length=60,
        fill_unknown=False,
    )

    assert result["levels"] == ("U" * 24) + "L"


def test_build_levels_payload_aggregates_from_existing_levels():
    """Test aggregation follows the main sensor's level decisions."""
    start = datetime(2025, 8, 9, 0, 0, tzinfo=UTC)
    rates = [
        _rate(start, 60, "Low"),
        _rate(start + timedelta(minutes=60), 60, "High"),
        _rate(start + timedelta(minutes=120), 60, "Low"),
        _rate(start + timedelta(minutes=180), 60, "Medium"),
    ]

    result = build_levels_payload_from_rates(
        rates,
        low_threshold=1.0,
        high_threshold=2.0,
        reference_time=datetime(2025, 8, 9, 12, 0, tzinfo=UTC),
        requested_length=120,
        fill_unknown=False,
    )

    assert result["levels"] == "HM"
