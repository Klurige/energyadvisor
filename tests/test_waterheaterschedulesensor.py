from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.helpers.entity import EntityCategory

from custom_components.energyadvisor.sensor.waterheaterschedulesensor import (
    WaterHeaterDiagnosticsSensor,
    WaterHeaterScheduleSensor,
)


def _schedule_sensor():
    hass = MagicMock()
    hass.config.time_zone = "UTC"
    hass.states.get.return_value = None
    entry = SimpleNamespace(entry_id="test", unique_id="test", options={})
    return WaterHeaterScheduleSensor(hass, entry, None, None), entry


def test_schedule_attributes_only_include_on_slots():
    schedule_sensor, _entry = _schedule_sensor()

    assert schedule_sensor.extra_state_attributes == {"on_slots": []}


def test_diagnostics_sensor_contains_schedule_diagnostics():
    schedule_sensor, entry = _schedule_sensor()
    diagnostics_sensor = WaterHeaterDiagnosticsSensor(entry, None, schedule_sensor)

    assert diagnostics_sensor.entity_category == EntityCategory.DIAGNOSTIC
    diagnostic_attributes = diagnostics_sensor.extra_state_attributes
    assert diagnostic_attributes.keys() == schedule_sensor.diagnostic_attributes.keys()
    assert "on_slots" not in diagnostic_attributes
    assert diagnostics_sensor.native_value == "incomplete"


def test_diagnostics_sensor_updates_with_schedule():
    schedule_sensor, entry = _schedule_sensor()
    diagnostics_sensor = WaterHeaterDiagnosticsSensor(entry, None, schedule_sensor)
    diagnostics_sensor.async_write_ha_state = MagicMock()
    schedule_sensor.async_write_ha_state = MagicMock()
    schedule_sensor.async_add_update_listener(diagnostics_sensor._handle_update)

    schedule_sensor._refresh_state_only()

    diagnostics_sensor.async_write_ha_state.assert_called_once()
