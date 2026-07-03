"""Tests for learned-state persistence in the battery charge mode sensor."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.energyadvisor.sensor.batterychargemodesensor import (
    BatteryChargeModeSensor,
)


class _FakeStore:
    def __init__(self, load_result):
        self.load_result = load_result
        self.saved_payloads = []

    async def async_load(self):
        return self.load_result

    async def async_save(self, payload):
        self.saved_payloads.append(payload)


def _make_sensor():
    hass = MagicMock()
    hass.config = MagicMock()
    entry = MagicMock()
    entry.entry_id = "battery-entry"
    entry.options = {}
    device_info = MagicMock()
    source_sensor = MagicMock()
    source_sensor.has_rates = False
    source_sensor.async_add_update_listener.return_value = lambda: None
    return BatteryChargeModeSensor(hass, entry, device_info, source_sensor)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "margin, expected_margin",
    [
        (1.2345, 1.2345),
        (-2.0, 0.0),
    ],
)
async def test_load_learned_data_restores_history_and_clamps_margin(
    margin, expected_margin
):
    sensor = _make_sensor()
    store = _FakeStore(
        {
            "base_load_history": [1.0, "bad", 0.0, -1.0, float("inf"), 3.0],
            "sell_safety_margin_kwh": margin,
        }
    )
    sensor._bootstrap_base_load_from_history = AsyncMock()

    with patch(
        "custom_components.energyadvisor.sensor.batterychargemodesensor.Store",
        return_value=store,
    ):
        await sensor._load_learned_data()

    assert sensor._store is store
    assert sensor._base_load_history == [1.0, 3.0]
    assert sensor._household_base_load_kw == pytest.approx(2.0)
    assert sensor._sell_safety_margin_kwh == expected_margin
    sensor._bootstrap_base_load_from_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_learned_data_bootstraps_when_history_missing():
    sensor = _make_sensor()
    store = _FakeStore({"sell_safety_margin_kwh": 0.75})
    sensor._bootstrap_base_load_from_history = AsyncMock()

    with patch(
        "custom_components.energyadvisor.sensor.batterychargemodesensor.Store",
        return_value=store,
    ):
        await sensor._load_learned_data()

    assert sensor._base_load_history == []
    assert sensor._sell_safety_margin_kwh == 0.75
    sensor._bootstrap_base_load_from_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_learned_data_persists_history_and_rounded_margin():
    sensor = _make_sensor()
    store = _FakeStore(None)
    sensor._store = store
    sensor._base_load_history = [1.2, 2.3, 3.4]
    sensor._sell_safety_margin_kwh = 1.23456

    await sensor._save_learned_data()

    assert store.saved_payloads == [
        {
            "base_load_history": [1.2, 2.3, 3.4],
            "sell_safety_margin_kwh": 1.235,
        }
    ]
