"""Tests for the Counter Demo config flow."""

import pytest

from custom_components.counter_demo.config_flow import CounterDemoConfigFlow


@pytest.mark.asyncio
async def test_config_flow_creates_entry(hass):
    flow = CounterDemoConfigFlow()
    flow.hass = hass

    result = await flow.async_step_user()

    assert result["type"] == "create_entry"
    assert result["title"] == "Counter Demo"
