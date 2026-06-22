"""Tests for service registration."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.energyadvisor.const import DOMAIN
from custom_components.energyadvisor.services import async_setup_services


@pytest.mark.asyncio
async def test_get_levels_service_handler_uses_requested_level_length() -> None:
    """Test service handler forwards level_length to the loaded sensor."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.has_service.return_value = False
    expected = {"level_length": 60, "levels": "LMH"}
    levels_sensor = MagicMock()
    levels_sensor.entity_id = "sensor.energy_advisor_price"
    levels_sensor.build_levels_payload.return_value = expected
    hass.data = {DOMAIN: {"entry": SimpleNamespace(levels_sensor=levels_sensor)}}

    async_setup_services(hass)
    handler = hass.services.async_register.call_args.args[2]

    call = MagicMock()
    call.data = {"level_length": 60}
    response = await handler(call)

    assert response == expected
    levels_sensor.build_levels_payload.assert_called_once_with(requested_length=60)


@pytest.mark.asyncio
async def test_get_levels_service_handler_defaults_level_length_to_zero() -> None:
    """Test service handler defaults level_length to 0 when omitted."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.has_service.return_value = False
    expected = {"level_length": 0, "levels": "U"}
    levels_sensor = MagicMock()
    levels_sensor.entity_id = "sensor.energy_advisor_price"
    levels_sensor.build_levels_payload.return_value = expected
    hass.data = {DOMAIN: {"entry": SimpleNamespace(levels_sensor=levels_sensor)}}

    async_setup_services(hass)
    handler = hass.services.async_register.call_args.args[2]

    call = MagicMock()
    call.data = {}
    response = await handler(call)

    assert response == expected
    levels_sensor.build_levels_payload.assert_called_once_with(requested_length=0)


@pytest.mark.asyncio
async def test_get_levels_service_handler_selects_requested_entity() -> None:
    """Test service handler can target a specific loaded sensor."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.has_service.return_value = False

    selected_sensor = MagicMock()
    selected_sensor.entity_id = "sensor.energy_advisor_price_2"
    selected_sensor.build_levels_payload.return_value = {
        "level_length": 15,
        "levels": "LM",
    }
    other_sensor = MagicMock()
    other_sensor.entity_id = "sensor.energy_advisor_price"

    hass.data = {
        DOMAIN: {
            "entry-1": SimpleNamespace(levels_sensor=other_sensor),
            "entry-2": SimpleNamespace(levels_sensor=selected_sensor),
        }
    }

    async_setup_services(hass)
    handler = hass.services.async_register.call_args.args[2]

    call = MagicMock()
    call.data = {"entity_id": "sensor.energy_advisor_price_2", "level_length": 15}
    response = await handler(call)

    assert response == {"level_length": 15, "levels": "LM"}
    selected_sensor.build_levels_payload.assert_called_once_with(requested_length=15)
    other_sensor.build_levels_payload.assert_not_called()


@pytest.mark.asyncio
async def test_get_levels_service_requires_entity_id_for_multiple_sensors() -> None:
    """Test service raises when multiple sensors are loaded without entity_id."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.has_service.return_value = False
    hass.data = {
        DOMAIN: {
            "entry-1": SimpleNamespace(
                levels_sensor=SimpleNamespace(entity_id="sensor.a")
            ),
            "entry-2": SimpleNamespace(
                levels_sensor=SimpleNamespace(entity_id="sensor.b")
            ),
        }
    }

    async_setup_services(hass)
    handler = hass.services.async_register.call_args.args[2]

    call = MagicMock()
    call.data = {}

    with pytest.raises(ServiceValidationError):
        await handler(call)
