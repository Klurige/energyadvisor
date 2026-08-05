"""Tests for the local Vattenfall-backed web server helpers."""

from datetime import date

import pytest

from scripts.web import vattenfall_local_server


def _build_vattenfall_payload(for_day: date) -> dict:
    """Create one day of 15-minute prices in Vattenfall API format."""
    prices = []
    for slot in range(96):
        hour, minute = divmod(slot * 15, 60)
        prices.append(
            {
                "year": for_day.year,
                "month": for_day.month,
                "day": for_day.day,
                "hour": hour,
                "minute": minute,
                "measurement": {"value": 25.0 + slot * 0.1},
            }
        )
    return {
        "deliveryAreas": "SE4",
        "currency": "SEK",
        "unit": "SWEDISH_ORE/KWH",
        "resolution": "15mins",
        "timezone": "CET",
        "prices": prices,
    }


@pytest.mark.asyncio
async def test_build_battery_charge_payload_uses_price_sensor_compact_rates(monkeypatch):
    """Battery plan payload should expose ratesCount without accessing private internals."""
    selected_day = date(2026, 8, 5)
    stub_payload = _build_vattenfall_payload(selected_day)

    monkeypatch.setattr(
        vattenfall_local_server,
        "_fetch_vattenfall_payload",
        lambda _delivery_start, _delivery_end: stub_payload,
    )

    payload = await vattenfall_local_server._build_battery_charge_payload(
        selected_day, requested_hours=24
    )

    assert payload["requestedHours"] == 24
    assert payload["ratesCount"] == 96
    assert payload["chargeEntries"]
    assert payload["unitOfMeasurement"] == "SEK/kWh"
