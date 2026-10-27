import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.energyadvisor import (
    async_setup_entry as integration_async_setup_entry,
    async_unload_entry,
)
from custom_components.energyadvisor.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_DEGRADATION_COST,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_MIN_SOC_PCT,
    CONF_DEBUG,
    CONF_EXCLUDE_FROM_RECORDING,
    CONF_NORDPOOL_PRICES_SENSOR,
    DOMAIN,
)
from custom_components.energyadvisor.coordinators import elpris_debug_coordinator
from custom_components.energyadvisor.coordinators.elpris_debug_coordinator import (
    ElprisDebugCoordinator,
)
from custom_components.energyadvisor.models import EnergyAdvisorRuntimeData
from custom_components.energyadvisor.sensor import (
    async_setup_entry as sensor_async_setup_entry,
)
from custom_components.energyadvisor.sensor.batterychargemodesensor import (
    BatteryChargeModeSensor,
)
from custom_components.energyadvisor.sensor.compactlevels import CompactLevelsSensor
from custom_components.energyadvisor.sensor.price import PriceSensor
from custom_components.energyadvisor.services import (
    SERVICE_DEBUG_SELECT_DATE,
    SERVICE_DEBUG_SELECT_DATE_SCHEMA,
    async_setup_debug_services,
    async_setup_services,
)

TZ_NAME = "Europe/Stockholm"
TZ = ZoneInfo(TZ_NAME)
PAST_DAY = datetime.date(2020, 1, 15)
PRICES_SENSOR = "sensor.nord_pool_se4_current_price"


def _hass() -> MagicMock:
    hass = MagicMock(spec=HomeAssistant)
    hass.config = MagicMock()
    hass.config.time_zone = TZ_NAME
    hass.data = {}
    hass.bus = MagicMock()
    nordpool_state = MagicMock(spec=State)
    nordpool_state.state = "1.23"
    nordpool_state.attributes = {
        "unit_of_measurement": "EUR/MWh",
        "prices_in_cents": True,
    }
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=nordpool_state)
    return hass


def _entry(**options) -> MagicMock:
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.unique_id = PRICES_SENSOR
    entry.options = {
        CONF_NORDPOOL_PRICES_SENSOR: PRICES_SENSOR,
        CONF_EXCLUDE_FROM_RECORDING: True,
        CONF_BATTERY_DEGRADATION_COST: 0.0,
        **options,
    }
    return entry


def _price_sensor(hass, entry) -> PriceSensor:
    sensor = PriceSensor(hass, entry, MagicMock(spec=DeviceInfo))
    sensor.hass = hass
    sensor.async_write_ha_state = MagicMock()
    return sensor


def _day_payload(day: datetime.date) -> dict:
    """A full day of 15 minute slots in currency/MWh.

    Cheapest at 03:00 and 13:00, most expensive at 08:00 and 19:00.
    """
    shape = {3: 100.0, 8: 900.0, 13: 150.0, 19: 1000.0}
    raw = []
    start = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=TZ)
    for index in range(96):
        slot_start = start + datetime.timedelta(minutes=15 * index)
        raw.append(
            {
                "start": slot_start.isoformat(),
                "end": (slot_start + datetime.timedelta(minutes=15)).isoformat(),
                "price": shape.get(slot_start.hour, 500.0)
                + (slot_start.minute / 15) * 0.1,
            }
        )
    return {"currency": "SEK", "raw": raw, "price_divisor": 1}


def _today() -> datetime.date:
    return datetime.datetime.now(TZ).date()


def _debug_coordinator(hass, price_sensor) -> ElprisDebugCoordinator:
    coordinator = ElprisDebugCoordinator(hass, price_sensor.async_update_data, "SE4")
    coordinator._is_running = True
    return coordinator


async def _select(coordinator: ElprisDebugCoordinator, day: datetime.date) -> None:
    """Select ``day`` using generated prices instead of the network."""
    with (
        patch.object(
            coordinator,
            "_async_fetch_day",
            new=AsyncMock(return_value=_day_payload(day)["raw"]),
        ),
        patch.object(elpris_debug_coordinator, "async_call_later"),
    ):
        await coordinator.async_select_date(day)


@pytest.mark.asyncio
async def test_selected_past_date_is_used_by_the_price_sensor_as_today() -> None:
    hass = _hass()
    sensor = _price_sensor(hass, _entry(**{CONF_DEBUG: True}))

    await _select(_debug_coordinator(hass, sensor), PAST_DAY)

    assert len(sensor._rates) == 96
    assert {rate["start"].date() for rate in sensor._rates} == {_today()}
    # A refresh purges everything before today; nothing is lost.
    await sensor._refresh_sensor_state()
    assert len(sensor._rates) == 96

    now = datetime.datetime.now(TZ)
    current = next(r for r in sensor._rates if r["start"] <= now < r["end"])
    assert sensor._level != "Unknown"
    assert sensor._spot_price == pytest.approx(current["spot_price"])
    assert sensor._spot_price > 0


@pytest.mark.asyncio
async def test_debug_price_sensor_ignores_nord_pool_units_and_divisor() -> None:
    sensor = _price_sensor(_hass(), _entry(**{CONF_DEBUG: True, "price_divisor": 100}))

    await sensor.async_update_data(_day_payload(_today()))

    assert sensor._price_divisor == 1
    assert sensor._currency == "SEK"
    # 100 SEK/MWh at 03:00 is 0.10 SEK/kWh.
    slot = next(r for r in sensor._rates if r["start"].hour == 3)
    assert slot["spot_price"] == pytest.approx(0.1, abs=0.001)


@pytest.mark.asyncio
async def test_debug_date_flows_to_battery_modes_and_compact_levels() -> None:
    hass = _hass()
    hass.async_create_background_task = lambda coro, name, **kwargs: (
        asyncio.ensure_future(coro)
    )
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    entry = _entry(
        **{
            CONF_DEBUG: True,
            CONF_BATTERY_CAPACITY_KWH: 15.0,
            CONF_BATTERY_MAX_CHARGE_POWER_W: 5000,
            CONF_BATTERY_MIN_SOC_PCT: 0,
        }
    )
    price_sensor = _price_sensor(hass, entry)
    battery_sensor = BatteryChargeModeSensor(
        hass, entry, MagicMock(spec=DeviceInfo), price_sensor
    )
    battery_sensor.async_write_ha_state = MagicMock()
    battery_sensor._now = lambda: datetime.datetime.combine(
        _today(), datetime.time(0, 0), tzinfo=TZ
    )
    price_sensor.async_add_update_listener(battery_sensor._handle_price_update)
    coordinator = _debug_coordinator(hass, price_sensor)

    await _select(coordinator, PAST_DAY)
    await battery_sensor._plan_task

    modes = battery_sensor.extra_state_attributes["modes"]
    assert len(modes) == 96
    by_mode: dict[str, list[str]] = {}
    for slot in modes:
        by_mode.setdefault(slot["mode"], []).append(slot["from"])
    # The planner charges in the cheap hours (03:00 and 13:00) and sells over
    # the expensive ones (08:00 and 19:00), never the other way round. Credit
    # equals cost in the debug prices, so the battery sells instead of
    # discharging.
    charge_hours = {t[11:13] for t in by_mode["charge"]}
    discharge_hours = {
        t[11:13] for mode in ("discharge", "sell") for t in by_mode.get(mode, [])
    }
    assert {"03", "13"} <= charge_hours
    assert {"08", "19"} <= discharge_hours
    assert not charge_hours & {"08", "19"}
    assert not discharge_hours & {"03", "13"}
    assert {m["from"][:10] for m in modes} == {_today().isoformat()}

    compact_sensor = CompactLevelsSensor(
        hass, entry, MagicMock(spec=DeviceInfo), price_sensor
    )
    compact_sensor.hass = hass
    _seconds, compact, _next_update = compact_sensor._fetch_compact_values()
    assert isinstance(compact, dict)
    assert set(price_sensor.build_levels_payload()["levels"]) - {"U"}


@pytest.mark.asyncio
async def test_debug_prices_follow_the_day_change() -> None:
    hass = _hass()
    sensor = _price_sensor(hass, _entry(**{CONF_DEBUG: True}))
    coordinator = _debug_coordinator(hass, sensor)
    await _select(coordinator, PAST_DAY)

    tomorrow = _today() + datetime.timedelta(days=1)
    coordinator._now = lambda: datetime.datetime.now(TZ) + datetime.timedelta(days=1)
    with patch.object(elpris_debug_coordinator, "async_call_later"):
        await coordinator._async_rollover()

    assert {rate["start"].date() for rate in sensor._rates} == {tomorrow}


@pytest.mark.asyncio
async def test_sensor_setup_in_debug_mode_uses_debug_coordinator() -> None:
    hass = _hass()
    entry = _entry(**{CONF_DEBUG: True, "currency": "EUR"})
    entry.async_on_unload = MagicMock()
    add_entities = MagicMock()

    with (
        patch(
            "custom_components.energyadvisor.sensor.async_get_integration",
            new=AsyncMock(return_value=MagicMock(version="1.0")),
        ),
        patch(
            "custom_components.energyadvisor.sensor._resolve_nordpool_config_entry_id"
        ) as resolve_nordpool,
        patch(
            "custom_components.energyadvisor.sensor.NordpoolDataCoordinator"
        ) as nordpool_coordinator,
        patch.object(ElprisDebugCoordinator, "start") as start,
    ):
        await sensor_async_setup_entry(hass, entry, add_entities)

    resolve_nordpool.assert_not_called()
    nordpool_coordinator.assert_not_called()
    start.assert_called_once_with()
    add_entities.assert_called_once()

    runtime_data = entry.runtime_data
    assert isinstance(runtime_data.debug_coordinator, ElprisDebugCoordinator)
    assert runtime_data.coordinator is runtime_data.debug_coordinator
    assert runtime_data.debug_coordinator.area == "SE4"
    assert runtime_data.debug_coordinator._currency == "EUR"
    assert runtime_data.debug_coordinator.data_update_callback == (
        runtime_data.levels_sensor.async_update_data
    )

    with patch.object(ElprisDebugCoordinator, "stop") as stop:
        entry.async_on_unload.call_args.args[0]()
    stop.assert_called_once_with()


@pytest.mark.asyncio
async def test_sensor_setup_without_debug_still_uses_nordpool() -> None:
    hass = _hass()
    entry = _entry()
    entry.async_on_unload = MagicMock()

    with (
        patch(
            "custom_components.energyadvisor.sensor.async_get_integration",
            new=AsyncMock(return_value=MagicMock(version="1.0")),
        ),
        patch(
            "custom_components.energyadvisor.sensor._resolve_nordpool_config_entry_id",
            return_value="nordpool_entry",
        ),
        patch(
            "custom_components.energyadvisor.sensor.NordpoolDataCoordinator"
        ) as nordpool_coordinator,
    ):
        await sensor_async_setup_entry(hass, entry, MagicMock())

    nordpool_coordinator.assert_called_once()
    assert entry.runtime_data.debug_coordinator is None
    nordpool_coordinator.return_value.start.assert_called_once_with()


def _runtime(entity_id: str, debug_coordinator) -> SimpleNamespace:
    return SimpleNamespace(
        levels_sensor=SimpleNamespace(entity_id=entity_id),
        debug_coordinator=debug_coordinator,
    )


async def test_debug_service_selects_date_on_the_debug_coordinator(hass) -> None:
    coordinator = MagicMock()
    coordinator.async_select_date = AsyncMock(return_value=96)
    hass.data[DOMAIN] = {"a": _runtime("sensor.price", coordinator)}
    async_setup_debug_services(hass)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_DEBUG_SELECT_DATE,
        {"date": "2026-10-24", "area": "se3"},
        blocking=True,
    )

    coordinator.async_select_date.assert_awaited_once_with(
        datetime.date(2026, 10, 24), "SE3"
    )


async def test_debug_service_is_not_registered_by_plain_setup(hass) -> None:
    async_setup_services(hass)

    assert hass.services.has_service(DOMAIN, "get_levels")
    assert not hass.services.has_service(DOMAIN, SERVICE_DEBUG_SELECT_DATE)


async def test_debug_service_validation_and_resolution_errors(hass) -> None:
    async_setup_debug_services(hass)

    with pytest.raises(ServiceValidationError, match="debug option"):
        await hass.services.async_call(
            DOMAIN, SERVICE_DEBUG_SELECT_DATE, {"date": "2026-10-24"}, blocking=True
        )

    first, second = MagicMock(), MagicMock()
    first.async_select_date = AsyncMock()
    second.async_select_date = AsyncMock()
    hass.data[DOMAIN] = {
        "a": _runtime("sensor.first", first),
        "b": _runtime("sensor.second", second),
    }

    with pytest.raises(ServiceValidationError, match="Multiple"):
        await hass.services.async_call(
            DOMAIN, SERVICE_DEBUG_SELECT_DATE, {"date": "2026-10-24"}, blocking=True
        )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_DEBUG_SELECT_DATE,
        {"date": "2026-10-24", "entity_id": "sensor.second"},
        blocking=True,
    )
    second.async_select_date.assert_awaited_once()
    first.async_select_date.assert_not_awaited()

    with pytest.raises(ServiceValidationError, match="Unknown"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_DEBUG_SELECT_DATE,
            {"date": "2026-10-24", "entity_id": "sensor.other"},
            blocking=True,
        )


def test_debug_service_schema_rejects_bad_input() -> None:
    assert SERVICE_DEBUG_SELECT_DATE_SCHEMA({"date": "2026-10-24"})["date"] == (
        datetime.date(2026, 10, 24)
    )
    with pytest.raises(vol.Invalid):
        SERVICE_DEBUG_SELECT_DATE_SCHEMA({})
    with pytest.raises(vol.Invalid):
        SERVICE_DEBUG_SELECT_DATE_SCHEMA({"date": "not-a-date"})
    with pytest.raises(vol.Invalid):
        SERVICE_DEBUG_SELECT_DATE_SCHEMA({"date": "2026-10-24", "area": "SE9"})


async def test_entry_setup_registers_debug_service_only_with_debug_coordinator(
    hass,
) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    entry.add_update_listener = MagicMock(return_value=lambda: None)
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    entry.runtime_data = EnergyAdvisorRuntimeData(
        levels_sensor=MagicMock(),
        compact_sensor=MagicMock(),
        coordinator=MagicMock(),
        debug_coordinator=MagicMock(),
    )

    with patch(
        "custom_components.energyadvisor._async_migrate_entity_registry",
        new=AsyncMock(),
    ):
        assert await integration_async_setup_entry(hass, entry) is True

    assert hass.services.has_service(DOMAIN, SERVICE_DEBUG_SELECT_DATE)


async def test_unload_removes_debug_service_only_after_last_debug_entry(hass) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    other_debug = SimpleNamespace(debug_coordinator=MagicMock())
    hass.data[DOMAIN] = {
        entry.entry_id: SimpleNamespace(debug_coordinator=MagicMock()),
        "other": other_debug,
    }
    async_setup_services(hass)
    async_setup_debug_services(hass)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    assert await async_unload_entry(hass, entry) is True
    assert hass.services.has_service(DOMAIN, SERVICE_DEBUG_SELECT_DATE)
    assert hass.services.has_service(DOMAIN, "get_levels")

    hass.data[DOMAIN]["other"] = SimpleNamespace(debug_coordinator=None)
    hass.data[DOMAIN][entry.entry_id] = SimpleNamespace(debug_coordinator=MagicMock())
    assert await async_unload_entry(hass, entry) is True
    assert not hass.services.has_service(DOMAIN, SERVICE_DEBUG_SELECT_DATE)
    assert hass.services.has_service(DOMAIN, "get_levels")
