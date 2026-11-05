from datetime import datetime, timedelta
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.core import State
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache_with_extra_data,
)

from custom_components.energyadvisor.const import (
    CONF_BATHROOM_HUMIDITY_ENTITY,
    CONF_WATER_HEATER_MAX_HOURS,
    CONF_WATER_HEATER_POWER_ENTITY,
    CONF_WATER_HEATER_POWER_W,
    DOMAIN,
    PREFERRED_SENSOR_ENTITY_IDS,
)
from custom_components.energyadvisor.sensor import waterheaterschedulesensor
from custom_components.energyadvisor.sensor.waterheaterschedulesensor import (
    PLAN_KEY,
    RESTORE_KEY,
    SAMPLE_RETENTION,
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


# -- sensor lifecycle with a real Home Assistant instance ----------------------

POWER_ENTITY = "sensor.water_heater_power"
HUMIDITY_ENTITY = "sensor.bathroom_humidity"
SCHEDULE_OPTIONS = {
    CONF_WATER_HEATER_POWER_ENTITY: POWER_ENTITY,
    CONF_BATHROOM_HUMIDITY_ENTITY: HUMIDITY_ENTITY,
    CONF_WATER_HEATER_POWER_W: 3000,
    CONF_WATER_HEATER_MAX_HOURS: 6,
}
STORE_KEY = f"{DOMAIN}.water_heater_samples.test"


def _local(*args: int) -> datetime:
    return datetime(*args, tzinfo=dt_util.get_default_time_zone())


def _window_rates(window_start: datetime) -> list[dict]:
    """96 quarter-hour prices for a 20:00-20:00 window, cheapest at night."""
    rates = []
    for index in range(96):
        start = window_start + timedelta(minutes=15 * index)
        rates.append(
            {
                "start": start,
                "end": start + timedelta(minutes=15),
                "cost": 1.0 + abs(index - 30) / 10,
            }
        )
    return rates


def _live_sensor(hass, rates: list[dict] | None = None) -> WaterHeaterScheduleSensor:
    hass.states.async_set(POWER_ENTITY, "0")
    hass.states.async_set(HUMIDITY_ENTITY, "50")
    entry = SimpleNamespace(entry_id="test", unique_id="test", options=SCHEDULE_OPTIONS)
    price_sensor = SimpleNamespace(
        _rates=rates or [],
        async_add_update_listener=MagicMock(return_value=lambda: None),
    )
    sensor = WaterHeaterScheduleSensor(hass, entry, None, price_sensor)
    sensor.async_write_ha_state = MagicMock()
    sensor.async_on_remove = MagicMock()
    return sensor


async def _add(hass, sensor: WaterHeaterScheduleSensor) -> None:
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()


async def _remove(sensor: WaterHeaterScheduleSensor) -> None:
    for call in sensor.async_on_remove.call_args_list:
        call.args[0]()
    await sensor.async_will_remove_from_hass()


def _restore(hass, payload: dict) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(PREFERRED_SENSOR_ENTITY_IDS["waterheaterschedule"], "off"),
                {"version": 1, RESTORE_KEY: payload},
            )
        ],
    )


async def test_first_start_plans_immediately_instead_of_at_planning_hour(
    hass, freezer
) -> None:
    freezer.move_to(_local(2026, 3, 2, 10, 0))
    window_start = _local(2026, 3, 1, 20, 0)
    sensor = _live_sensor(hass, _window_rates(window_start))

    await _add(hass, sensor)

    plan = sensor._plan
    assert plan is not None
    assert plan.window_start == window_start
    # No history yet: the default runtime of 4 h is planned in the time left.
    assert len(plan.selected_slots) == 16
    assert all(slot.end > dt_util.now() for slot in plan.selected_slots)
    await _remove(sensor)


async def test_restored_plan_from_an_ended_window_is_replaced(hass, freezer) -> None:
    freezer.move_to(_local(2026, 3, 2, 10, 0))
    old_start = _local(2026, 2, 27, 20, 0)
    _restore(
        hass,
        {
            PLAN_KEY: {
                "window_start": old_start.timestamp(),
                "window_end": (old_start + timedelta(days=1)).timestamp(),
                "category": "no_shower",
                "shower_detected_today": False,
                "required_runtime_hours": 0.25,
                "required_slots": 1,
                "expected_cost": 1.0,
                "runtime_capped": False,
                "complete": True,
                "incomplete_reason": None,
                "selected_slots": [
                    {
                        "start": old_start.timestamp(),
                        "end": (old_start + timedelta(minutes=15)).timestamp(),
                        "cost": 1.0,
                    }
                ],
            }
        },
    )
    sensor = _live_sensor(hass, _window_rates(_local(2026, 3, 1, 20, 0)))

    await _add(hass, sensor)

    assert sensor._plan.window_end == _local(2026, 3, 2, 20, 0)
    assert sensor._plan.selected_slots
    await _remove(sensor)


async def test_plan_without_prices_is_retried_but_a_real_plan_stays_fixed(
    hass, freezer
) -> None:
    freezer.move_to(_local(2026, 3, 2, 10, 0))
    sensor = _live_sensor(hass, rates=[])
    await _add(hass, sensor)
    assert sensor._plan.selected_slots == ()
    assert sensor._plan.incomplete_reason == "insufficient_slots"
    assert not sensor.available

    sensor._price_sensor._rates = _window_rates(_local(2026, 3, 1, 20, 0))
    price_listener = sensor._price_sensor.async_add_update_listener.call_args.args[0]
    price_listener()
    await hass.async_block_till_done()
    plan = sensor._plan
    assert len(plan.selected_slots) == 16
    assert sensor.available

    # New prices mid-window must not cause the heater to run a second time.
    sensor._price_sensor._rates = [
        {**rate, "cost": 10 - rate["cost"]} for rate in sensor._price_sensor._rates
    ]
    price_listener()
    await hass.async_block_till_done()
    assert sensor._plan is plan
    await _remove(sensor)


async def test_learning_runs_in_the_executor(hass, freezer) -> None:
    freezer.move_to(_local(2026, 3, 2, 10, 0))
    sensor = _live_sensor(hass, _window_rates(_local(2026, 3, 1, 20, 0)))
    loop_thread = threading.get_ident()
    learn_threads: list[int] = []
    real_learn = waterheaterschedulesensor._learn_from_samples

    def recording_learn(*args):
        learn_threads.append(threading.get_ident())
        return real_learn(*args)

    with patch.object(
        waterheaterschedulesensor, "_learn_from_samples", recording_learn
    ):
        await _add(hass, sensor)

    assert len(learn_threads) == 1
    assert learn_threads[0] != loop_thread
    await _remove(sensor)


async def test_power_samples_are_stored_outside_the_restore_state(
    hass, hass_storage, freezer
) -> None:
    freezer.move_to(_local(2026, 3, 2, 10, 0))
    legacy_ts = _local(2026, 3, 1, 12, 0).timestamp()
    _restore(hass, {"power_samples": [[legacy_ts, 2500.0], ["bad", 1.0]]})
    sensor = _live_sensor(hass)
    await _add(hass, sensor)

    # Samples saved by older versions in the restore state are migrated; the
    # invalid row is dropped and the current power is sampled on startup.
    samples = list(sensor._power_samples)
    assert samples[0] == (_local(2026, 3, 1, 12, 0), 2500.0)
    assert all(value == 0.0 for _ts, value in samples[1:])
    assert "power_samples" not in sensor.extra_restore_state_data.as_dict()[RESTORE_KEY]

    await _remove(sensor)
    stored = hass_storage[STORE_KEY]["data"]
    assert stored["t"][0] == legacy_ts
    assert stored["v"] == [value for _ts, value in samples]

    # After a restart the samples are loaded from the store.
    _restore(hass, {})
    restarted = _live_sensor(hass)
    await _add(hass, restarted)
    assert list(restarted._power_samples)[: len(samples)] == samples
    await _remove(restarted)


def test_expired_and_out_of_order_power_samples_are_dropped() -> None:
    sensor, _entry = _schedule_sensor()
    start = _local(2026, 1, 1, 0, 0)
    sensor._add_power_sample(start, 1.0)
    sensor._add_power_sample(start + timedelta(days=1), 2.0)
    sensor._add_power_sample(start + timedelta(hours=1), 3.0)  # clock went back

    sensor._add_power_sample(start + SAMPLE_RETENTION + timedelta(hours=12), 4.0)

    assert [value for _ts, value in sensor._power_samples] == [2.0, 4.0]
