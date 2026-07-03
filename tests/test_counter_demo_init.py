"""Tests for the Counter Demo integration setup."""

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.counter_demo import PLATFORMS, async_setup_entry, async_unload_entry
from custom_components.counter_demo.const import DOMAIN


async def test_async_setup_entry_forwards_to_sensor_platform(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    assert await async_setup_entry(hass, entry) is True

    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, PLATFORMS
    )


async def test_async_unload_entry_unloads_sensor_platform(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    assert await async_unload_entry(hass, entry) is True

    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry, PLATFORMS
    )
