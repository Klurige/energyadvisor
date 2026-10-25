"""Services for the Energy Advisor integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinators.elpris_debug_coordinator import (
    ElprisDebugCoordinator,
    VALID_AREAS,
)

_LOGGER = logging.getLogger(__name__)

ATTR_ENTITY_ID = "entity_id"
ATTR_LEVEL_LENGTH = "level_length"
ATTR_DATE = "date"
ATTR_AREA = "area"
SERVICE_GET_LEVELS = "get_levels"
SERVICE_DEBUG_SELECT_DATE = "debug_select_date"
SERVICE_GET_LEVELS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_LEVEL_LENGTH, default=0): vol.All(
            cv.positive_int, vol.Coerce(int)
        ),
    }
)
SERVICE_DEBUG_SELECT_DATE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DATE): cv.date,
        vol.Optional(ATTR_AREA): vol.All(cv.string, vol.Upper, vol.In(VALID_AREAS)),
        vol.Optional(ATTR_ENTITY_ID): cv.entity_id,
    }
)


def _resolve_levels_sensor(hass: HomeAssistant, entity_id: str | None) -> object:
    """Resolve the target levels sensor from runtime data."""
    runtime_entries = hass.data.get(DOMAIN, {})
    matches = []

    for runtime_data in runtime_entries.values():
        levels_sensor = getattr(runtime_data, "levels_sensor", None)
        if levels_sensor is None:
            continue
        if entity_id is None or levels_sensor.entity_id == entity_id:
            matches.append(levels_sensor)

    if entity_id is not None:
        if matches:
            return matches[0]
        raise ServiceValidationError(
            f"Unknown energyadvisor sensor entity_id: {entity_id}"
        )

    if len(matches) == 1:
        return matches[0]

    if not matches:
        raise ServiceValidationError("No energyadvisor sensor is loaded.")

    raise ServiceValidationError(
        "Multiple energyadvisor sensors are loaded. "
        "Specify entity_id in the service call."
    )


@callback
def async_setup_debug_services(hass: HomeAssistant) -> None:
    """Register the debug date action once; only used in debug mode."""
    if hass.services.has_service(DOMAIN, SERVICE_DEBUG_SELECT_DATE):
        return

    async def debug_select_date(call: ServiceCall) -> None:
        coordinator = _resolve_debug_coordinator(hass, call.data.get(ATTR_ENTITY_ID))
        await coordinator.async_select_date(
            call.data[ATTR_DATE], call.data.get(ATTR_AREA)
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DEBUG_SELECT_DATE,
        debug_select_date,
        schema=SERVICE_DEBUG_SELECT_DATE_SCHEMA,
    )


@callback
def async_remove_debug_services(hass: HomeAssistant) -> None:
    """Remove the debug date action."""
    hass.services.async_remove(DOMAIN, SERVICE_DEBUG_SELECT_DATE)


def _resolve_debug_coordinator(
    hass: HomeAssistant, entity_id: str | None
) -> ElprisDebugCoordinator:
    """Resolve the debug coordinator of the (optionally given) price sensor."""
    matches = []
    for runtime_data in hass.data.get(DOMAIN, {}).values():
        debug_coordinator = getattr(runtime_data, "debug_coordinator", None)
        if debug_coordinator is None:
            continue
        levels_sensor = getattr(runtime_data, "levels_sensor", None)
        if entity_id is None or getattr(levels_sensor, "entity_id", None) == entity_id:
            matches.append(debug_coordinator)

    if len(matches) == 1 or (entity_id is not None and matches):
        return matches[0]
    if not matches:
        raise ServiceValidationError(
            "No energyadvisor entry has the debug option set."
            if entity_id is None
            else f"Unknown energyadvisor sensor entity_id: {entity_id}"
        )
    raise ServiceValidationError(
        "Multiple energyadvisor entries are in debug mode. "
        "Specify entity_id in the service call."
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register domain services once."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_LEVELS):
        return

    async def get_levels(call: ServiceCall) -> ServiceResponse:
        entity_id = call.data.get(ATTR_ENTITY_ID)
        requested_length = call.data.get(ATTR_LEVEL_LENGTH, 0)
        _LOGGER.debug(
            "Received service call to get levels for %s with level_length=%s",
            entity_id,
            requested_length,
        )
        levels_sensor = _resolve_levels_sensor(hass, entity_id)
        return levels_sensor.build_levels_payload(requested_length=requested_length)

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_LEVELS,
        get_levels,
        schema=SERVICE_GET_LEVELS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
