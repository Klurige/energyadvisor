"""Tests for the household forecast coordinator."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.energyadvisor.const import (
    CONF_BATTERY_SOC_ENTITY,
    CONF_CENTRAL_HEATING_ACTIVE_ENTITY,
    CONF_CENTRAL_HEATING_POWER_ENTITY,
    CONF_CENTRAL_HEATING_POWER_W,
    CONF_DEHUMIDIFIER_POWER_ENTITY,
    CONF_DEHUMIDIFIER_POWER_W,
    CONF_POWER_METER_CONSUMPTION,
    CONF_POOL_PUMP_POWER_ENTITY,
    CONF_POOL_PUMP_POWER_W,
    CONF_WATER_HEATER_ACTIVE_ENTITY,
    CONF_WATER_HEATER_POWER_ENTITY,
    CONF_WATER_HEATER_POWER_W,
)
from custom_components.energyadvisor.coordinators.household_forecast_coordinator import (
    DB_SCHEMA_VERSION,
    FORECAST_SLOT_COUNT,
    STATIC_LOAD_FORECAST_KW,
    STATIC_REASON,
    HouseholdForecastCoordinator,
)

UTC = timezone.utc


def _make_coordinator(
    tmp_path: Path,
    *,
    with_required_entities: bool = True,
    extra_options: dict[str, str] | None = None,
) -> tuple[HouseholdForecastCoordinator, MagicMock]:
    """Create a coordinator with a lightweight Home Assistant/config stub."""
    hass = MagicMock()
    hass.async_create_task = MagicMock(side_effect=lambda coro: None)
    hass.async_add_executor_job = AsyncMock(side_effect=lambda func, *args: func(*args))
    hass.config = SimpleNamespace(config_dir=str(tmp_path))
    hass.states = MagicMock()

    entry = MagicMock()
    entry.entry_id = "entry-id"
    entry.options = {}
    if with_required_entities:
        entry.options.update(
            {
                CONF_POWER_METER_CONSUMPTION: "sensor.household_meter",
                CONF_WATER_HEATER_ACTIVE_ENTITY: "binary_sensor.water_heater_active",
                CONF_CENTRAL_HEATING_ACTIVE_ENTITY: "binary_sensor.central_heating_active",
            }
        )
    if extra_options:
        entry.options.update(extra_options)
    return HouseholdForecastCoordinator(hass, entry), hass


@pytest.mark.asyncio
async def test_coordinator_creates_sqlite_schema_and_capture_targets(tmp_path: Path) -> None:
    """The coordinator should open the SQLite history file and wire capture targets."""
    coordinator, _hass = _make_coordinator(
        tmp_path,
        extra_options={CONF_BATTERY_SOC_ENTITY: "sensor.battery_soc"},
    )

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=store,
        ) as _mock_store_class,
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ) as mock_state_change,
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ) as mock_time_change,
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ) as mock_time_interval,
    ):
        coordinator._store = store
        await coordinator.async_setup()

    db_path = Path(coordinator._db_path())
    assert db_path.exists()

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "raw_samples",
            "interval_energy",
            "raw_events",
            "slot_rows",
            "forecast_runs",
            "meta",
        }.issubset(tables)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION

    assert len(coordinator._listeners) == 4
    assert len(coordinator._capture_listeners) == 2
    assert coordinator._capture_targets["sensor.household_meter"].key == (
        CONF_POWER_METER_CONSUMPTION
    )
    assert coordinator._capture_targets["binary_sensor.water_heater_active"].is_event
    assert not coordinator._capture_targets["sensor.battery_soc"].is_event
    assert coordinator._capture_targets["sensor.household_meter"].poll_interval == timedelta(
        seconds=60
    )
    assert coordinator._capture_targets["sensor.battery_soc"].poll_interval == timedelta(
        seconds=120
    )
    assert mock_state_change.call_count == 2
    assert mock_time_change.call_count == 3
    assert mock_time_interval.call_count == 1


@pytest.mark.asyncio
async def test_coordinator_starts_with_meter_only_optional_sensors_missing(
    tmp_path: Path,
) -> None:
    """The coordinator should still start when only the household meter is set."""
    coordinator, _hass = _make_coordinator(
        tmp_path,
        with_required_entities=False,
        extra_options={CONF_POWER_METER_CONSUMPTION: "sensor.household_meter"},
    )

    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=store,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ) as mock_state_change,
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ) as mock_time_change,
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ) as mock_time_interval,
    ):
        coordinator._store = store
        await coordinator.async_setup()

    assert len(coordinator._listeners) == 3
    assert len(coordinator._capture_listeners) == 2
    assert coordinator._capture_targets["sensor.household_meter"].key == (
        CONF_POWER_METER_CONSUMPTION
    )
    assert mock_state_change.call_count == 1
    assert mock_time_change.call_count == 3
    assert mock_time_interval.call_count == 1


@pytest.mark.asyncio
async def test_coordinator_persists_raw_history_across_restart(tmp_path: Path) -> None:
    """Raw samples should survive a restart and reload into the cache."""
    coordinator, _hass = _make_coordinator(tmp_path)
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=store,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ),
    ):
        coordinator._store = store
        await coordinator.async_setup()

    meter_target = coordinator._capture_targets["sensor.household_meter"]
    water_target = coordinator._capture_targets["binary_sensor.water_heater_active"]

    first_ts = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    meter_state = SimpleNamespace(
        entity_id=meter_target.entity_id,
        state="1.250",
        attributes={"unit_of_measurement": "kWh"},
        last_updated=first_ts,
    )
    water_state = SimpleNamespace(
        entity_id=water_target.entity_id,
        state="on",
        attributes={},
        last_updated=first_ts,
    )

    await coordinator._async_capture_target(meter_target, meter_state, source="event")
    await coordinator._async_capture_target(water_target, water_state, source="event")

    with sqlite3.connect(coordinator._db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_samples").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 1

    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state="1.250",
            attributes={"unit_of_measurement": "kWh"},
            last_updated=first_ts + timedelta(seconds=60),
        ),
        source="poll",
        captured_at=first_ts + timedelta(seconds=60),
    )

    with sqlite3.connect(coordinator._db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_samples").fetchone()[0] == 1

    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state="1.250",
            attributes={"unit_of_measurement": "kWh"},
            last_updated=first_ts + timedelta(seconds=121),
        ),
        source="poll",
        captured_at=first_ts + timedelta(seconds=121),
    )

    with sqlite3.connect(coordinator._db_path()) as conn:
        rows = conn.execute(
            "SELECT ts_utc, value, unit, quality FROM raw_samples ORDER BY ts_utc"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][1:] == (1.25, "kWh", "ok")
        assert rows[1][1:] == (1.25, "kWh", "heartbeat")

    await coordinator.async_shutdown()

    restart_coordinator, _restart_hass = _make_coordinator(tmp_path)
    restart_store = MagicMock()
    restart_store.async_load = AsyncMock(return_value=None)
    restart_store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=restart_store,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ),
    ):
        restart_coordinator._store = restart_store
        await restart_coordinator.async_setup()

    latest_meter = restart_coordinator._latest_numeric_samples[
        "power_meter_consumption"
    ]
    latest_water = restart_coordinator._latest_event_samples[
        "water_heater_active_entity"
    ]

    assert latest_meter.value == 1.25
    assert latest_meter.unit == "kWh"
    assert latest_meter.ts_utc == first_ts + timedelta(seconds=121)
    assert latest_water.state_num == 1.0
    assert latest_water.state_text == "on"


@pytest.mark.asyncio
async def test_coordinator_restarts_with_cold_start_forecast(tmp_path: Path) -> None:
    """Restarting without enough finalized slots should keep the cold-start shell."""
    coordinator, _hass = _make_coordinator(tmp_path)
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=store,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ),
    ):
        coordinator._store = store
        await coordinator.async_setup()

    assert coordinator.load_forecast_kw == pytest.approx(STATIC_LOAD_FORECAST_KW)
    assert coordinator.forecast_slots[0]["load"] == pytest.approx(
        STATIC_LOAD_FORECAST_KW
    )
    assert coordinator.reason == STATIC_REASON

    await coordinator.async_shutdown()

    restart_coordinator, _restart_hass = _make_coordinator(tmp_path)
    restart_store = MagicMock()
    restart_store.async_load = AsyncMock(return_value=None)
    restart_store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=restart_store,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ),
    ):
        restart_coordinator._store = restart_store
        await restart_coordinator.async_setup()

    assert restart_coordinator.load_forecast_kw == pytest.approx(
        STATIC_LOAD_FORECAST_KW
    )
    assert restart_coordinator.forecast_slots[0]["load"] == pytest.approx(
        STATIC_LOAD_FORECAST_KW
    )
    assert restart_coordinator.reason == STATIC_REASON


@pytest.mark.asyncio
async def test_coordinator_finalizes_interval_energy_and_slot_rows(tmp_path: Path) -> None:
    """Power samples should become interval energy and split across slots."""
    coordinator, _hass = _make_coordinator(tmp_path)
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=store,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ),
    ):
        coordinator._store = store
        await coordinator.async_setup()

    meter_target = coordinator._capture_targets["sensor.household_meter"]
    first_ts = datetime(2026, 9, 5, 12, 7, 30, tzinfo=UTC)
    second_ts = datetime(2026, 9, 5, 12, 17, 30, tzinfo=UTC)

    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state="1000",
            attributes={"unit_of_measurement": "W"},
            last_updated=first_ts,
        ),
        source="event",
    )
    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state="2000",
            attributes={"unit_of_measurement": "W"},
            last_updated=second_ts,
        ),
        source="event",
    )

    assert await coordinator._async_finalize_history(
        datetime(2026, 9, 5, 12, 30, tzinfo=UTC)
    )

    with sqlite3.connect(coordinator._db_path()) as conn:
        interval_rows = conn.execute(
            "SELECT ts_from_utc, ts_to_utc, delta_kwh, interval_sec, interval_kw, quality "
            "FROM interval_energy WHERE sensor_key = ? ORDER BY ts_from_utc",
            (CONF_POWER_METER_CONSUMPTION,),
        ).fetchall()
        assert len(interval_rows) == 1
        assert interval_rows[0][0] == first_ts.isoformat(timespec="seconds")
        assert interval_rows[0][1] == second_ts.isoformat(timespec="seconds")
        assert interval_rows[0][2] == pytest.approx(0.25)
        assert interval_rows[0][3] == 600
        assert interval_rows[0][4] == pytest.approx(1.5)
        assert interval_rows[0][5] == "sparse_gap"

        slot_rows = conn.execute(
            "SELECT slot_start_utc, slot_energy_kwh, load_kw, sample_count, "
            "quality_score, features_json FROM slot_rows ORDER BY slot_start_utc"
        ).fetchall()
        assert [row[0] for row in slot_rows] == [
            "2026-09-05T12:00:00+00:00",
            "2026-09-05T12:15:00+00:00",
        ]
        assert slot_rows[0][1] == pytest.approx(0.1875)
        assert slot_rows[0][2] == pytest.approx(0.75)
        assert slot_rows[0][3] == 1
        assert slot_rows[0][4] == pytest.approx(0.25)
        assert json.loads(slot_rows[0][5])["required_missing"] is True
        assert slot_rows[1][1] == pytest.approx(0.0625)
        assert slot_rows[1][2] == pytest.approx(0.25)
        assert slot_rows[1][3] == 1
        assert slot_rows[1][4] == pytest.approx(1 / 12)
        assert json.loads(slot_rows[1][5])["required_missing"] is True

        assert conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("last_finalized_slot_utc",),
        ).fetchone()[0] == "2026-09-05T12:15:00+00:00"
        assert conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("last_generation_utc",),
        ).fetchone()[0] == "2026-09-05T12:30:00+00:00"
        latest_generation = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("last_generation_utc",),
        ).fetchone()[0]
        assert latest_generation == "2026-09-05T12:30:00+00:00"
        forecast_run_count = conn.execute(
            "SELECT COUNT(*) FROM forecast_runs WHERE generated_at_utc = ?",
            (latest_generation,),
        ).fetchone()[0]
        assert forecast_run_count == FORECAST_SLOT_COUNT
        assert conn.execute(
            "SELECT load_kw FROM forecast_runs "
            "WHERE generated_at_utc = ? ORDER BY slot_start_utc LIMIT 1",
            (latest_generation,),
        ).fetchone()[0] == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_coordinator_subtracts_known_appliance_loads_from_slot_rows(
    tmp_path: Path,
) -> None:
    """Household slots should subtract the configured appliance power sensors."""
    coordinator, _hass = _make_coordinator(
        tmp_path,
        extra_options={
            CONF_WATER_HEATER_POWER_ENTITY: "sensor.water_heater_power",
            CONF_WATER_HEATER_POWER_W: 4000.0,
            CONF_CENTRAL_HEATING_POWER_ENTITY: "sensor.central_heating_power",
            CONF_CENTRAL_HEATING_POWER_W: 3000.0,
            CONF_POOL_PUMP_POWER_ENTITY: "sensor.pool_pump_power",
            CONF_POOL_PUMP_POWER_W: 1500.0,
            CONF_DEHUMIDIFIER_POWER_ENTITY: "sensor.dehumidifier_power",
            CONF_DEHUMIDIFIER_POWER_W: 2000.0,
        },
    )
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=store,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ),
    ):
        coordinator._store = store
        await coordinator.async_setup()

    meter_target = coordinator._capture_targets["sensor.household_meter"]
    water_target = coordinator._capture_targets["sensor.water_heater_power"]
    central_target = coordinator._capture_targets["sensor.central_heating_power"]
    pool_target = coordinator._capture_targets["sensor.pool_pump_power"]
    dehumidifier_target = coordinator._capture_targets["sensor.dehumidifier_power"]
    water_active_target = coordinator._capture_targets[
        "binary_sensor.water_heater_active"
    ]
    heating_active_target = coordinator._capture_targets[
        "binary_sensor.central_heating_active"
    ]

    first_ts = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    second_ts = datetime(2026, 9, 5, 12, 15, tzinfo=UTC)

    async def _capture_pair(target, first_state: str, second_state: str) -> None:
        await coordinator._async_capture_target(
            target,
            SimpleNamespace(
                entity_id=target.entity_id,
                state=first_state,
                attributes={"unit_of_measurement": "W"},
                last_updated=first_ts,
            ),
            source="event",
        )
        await coordinator._async_capture_target(
            target,
            SimpleNamespace(
                entity_id=target.entity_id,
                state=second_state,
                attributes={"unit_of_measurement": "W"},
                last_updated=second_ts,
            ),
            source="event",
        )

    await _capture_pair(meter_target, "4000", "4000")
    await _capture_pair(water_target, "1000", "1000")
    await _capture_pair(central_target, "500", "500")
    await _capture_pair(pool_target, "500", "500")
    await _capture_pair(dehumidifier_target, "250", "250")

    await coordinator._async_capture_target(
        water_active_target,
        SimpleNamespace(
            entity_id=water_active_target.entity_id,
            state="on",
            attributes={},
            last_updated=first_ts,
        ),
        source="event",
    )
    await coordinator._async_capture_target(
        water_active_target,
        SimpleNamespace(
            entity_id=water_active_target.entity_id,
            state="on",
            attributes={},
            last_updated=second_ts,
        ),
        source="event",
    )
    await coordinator._async_capture_target(
        heating_active_target,
        SimpleNamespace(
            entity_id=heating_active_target.entity_id,
            state="on",
            attributes={},
            last_updated=first_ts,
        ),
        source="event",
    )
    await coordinator._async_capture_target(
        heating_active_target,
        SimpleNamespace(
            entity_id=heating_active_target.entity_id,
            state="on",
            attributes={},
            last_updated=second_ts,
        ),
        source="event",
    )

    assert await coordinator._async_finalize_history(
        datetime(2026, 9, 5, 12, 30, tzinfo=UTC)
    )

    with sqlite3.connect(coordinator._db_path()) as conn:
        slot_row = conn.execute(
            "SELECT slot_energy_kwh, load_kw, sample_count, quality_score, "
            "features_json FROM slot_rows ORDER BY slot_start_utc"
        ).fetchone()
        assert slot_row[0] == pytest.approx(0.4375)
        assert slot_row[1] == pytest.approx(1.75)
        assert slot_row[2] == 1
        assert slot_row[3] == pytest.approx(0.5)
        features = json.loads(slot_row[4])
        assert features["gross_load_kwh"] == pytest.approx(1.0)
        assert features["known_load_kwh"] == pytest.approx(0.5625)
        assert features["known_load_kw"] == pytest.approx(2.25)
        assert features["event_flags"]["water_heater_active"] == 1
        assert features["event_flags"]["central_heating_active"] == 1


@pytest.mark.asyncio
async def test_coordinator_marks_sparse_interval_quality(tmp_path: Path) -> None:
    """Long energy-counter gaps should be flagged as sparse in slot rows."""
    coordinator, _hass = _make_coordinator(tmp_path)
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=store,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ),
    ):
        coordinator._store = store
        await coordinator.async_setup()

    meter_target = coordinator._capture_targets["sensor.household_meter"]
    first_ts = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    second_ts = datetime(2026, 9, 5, 12, 8, tzinfo=UTC)

    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state="1.0",
            attributes={"unit_of_measurement": "kWh"},
            last_updated=first_ts,
        ),
        source="event",
    )
    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state="2.0",
            attributes={"unit_of_measurement": "kWh"},
            last_updated=second_ts,
        ),
        source="event",
    )

    assert await coordinator._async_finalize_history(
        datetime(2026, 9, 5, 12, 15, tzinfo=UTC)
    )

    with sqlite3.connect(coordinator._db_path()) as conn:
        assert conn.execute(
            "SELECT quality FROM interval_energy WHERE sensor_key = ?",
            (CONF_POWER_METER_CONSUMPTION,),
        ).fetchone()[0] == "sparse_gap"

        slot_row = conn.execute(
            "SELECT slot_energy_kwh, load_kw, sample_count, quality_score, "
            "features_json FROM slot_rows ORDER BY slot_start_utc"
        ).fetchone()
        assert slot_row[0] == pytest.approx(1.0)
        assert slot_row[1] == pytest.approx(4.0)
        assert slot_row[2] == 1
        assert 0.0 < slot_row[3] < 0.5
        features = json.loads(slot_row[4])
        assert features["sparse_interval_count"] == 1
        assert features["required_missing"] is True


@pytest.mark.asyncio
async def test_coordinator_builds_learned_forecast_from_slot_history(
    tmp_path: Path,
) -> None:
    """Persisted slot history should produce a learned seasonal forecast."""
    coordinator, _hass = _make_coordinator(tmp_path)
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=store,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ),
        patch.object(
            coordinator,
            "_slot_day_type",
            return_value="workday",
        ),
    ):
        coordinator._store = store
        await coordinator.async_setup()

    db = coordinator._ensure_db()
    base_day = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    for day_offset in range(8):
        for slot_index, load_kw in ((0, 0.72), (1, 1.28), (2, 0.88), (3, 0.94)):
            slot_start = base_day + timedelta(days=day_offset, minutes=15 * slot_index)
            db.execute(
                "INSERT OR REPLACE INTO slot_rows "
                "(slot_start_utc, slot_energy_kwh, load_kw, sample_count, "
                "quality_score, features_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    slot_start.isoformat(timespec="seconds"),
                    load_kw * 0.25,
                    load_kw,
                    1,
                    1.0,
                    json.dumps(
                        {
                            "slot_index": slot_index,
                            "day_type": "workday",
                            "is_holiday": False,
                            "interval_count": 1,
                            "sparse_interval_count": 0,
                            "required_missing": False,
                            "outdoor_temp_c": None,
                            "event_flags": {
                                "water_heater_active": 0,
                                "central_heating_active": 0,
                            },
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
    db.commit()

    assert await coordinator._async_finalize_history(
        datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    )

    with sqlite3.connect(coordinator._db_path()) as conn:
        latest_generation = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("last_generation_utc",),
        ).fetchone()[0]
        forecast_rows = conn.execute(
            "SELECT load_kw FROM forecast_runs WHERE generated_at_utc = ? "
            "ORDER BY slot_start_utc LIMIT 2",
            (latest_generation,),
        ).fetchall()
        assert len(forecast_rows) == 2
        assert forecast_rows[0][0] == pytest.approx(0.72)
        assert forecast_rows[1][0] == pytest.approx(1.28)
        assert conn.execute(
            "SELECT COUNT(*) FROM forecast_runs WHERE generated_at_utc = ?",
            (latest_generation,),
        ).fetchone()[0] == FORECAST_SLOT_COUNT

    assert coordinator.load_forecast_kw == pytest.approx(0.72)
    assert coordinator.household_load_forecast_w == pytest.approx(720.0)
    assert coordinator.learning_nights == 8
    assert coordinator.data_since == dt_util.as_local(base_day).date().isoformat()
    assert coordinator.last_sample_date == dt_util.as_local(
        base_day + timedelta(days=7, minutes=45)
    ).date().isoformat()
    assert coordinator.last_sample_kw == pytest.approx(0.94)
    assert "seasonal baseline" in coordinator.reason
    assert coordinator.forecast_slots[0]["load"] == pytest.approx(0.72)
    assert coordinator.forecast_slots[1]["load"] == pytest.approx(1.28)
    assert coordinator.forecast_slots[0]["load"] != coordinator.forecast_slots[1]["load"]


def test_coordinator_keeps_historical_forecast_slots_stable_on_refresh(
    tmp_path: Path,
) -> None:
    """Refreshes should only rewrite the current and future forecast slots."""
    coordinator, _hass = _make_coordinator(tmp_path)
    now_utc = datetime(2026, 9, 7, 20, 0, tzinfo=UTC)
    slot_starts_utc = coordinator._forecast_slot_starts_utc(now_utc)
    current_slot_start_utc = coordinator._floor_to_slot_start_utc(now_utc)
    historical_count = sum(
        1 for slot_start_utc in slot_starts_utc if slot_start_utc < current_slot_start_utc
    )

    coordinator._forecast_slots = [
        {
            "from": dt_util.as_local(slot_start_utc).strftime("%Y-%m-%dT%H:%M"),
            "load": float(index),
        }
        for index, slot_start_utc in enumerate(slot_starts_utc)
    ]
    fresh_slots = [
        {
            "from": dt_util.as_local(slot_start_utc).strftime("%Y-%m-%dT%H:%M"),
            "load": float(1000 + index),
        }
        for index, slot_start_utc in enumerate(slot_starts_utc)
    ]

    merged_slots = coordinator._preserve_historical_forecast_slots(
        fresh_slots,
        slot_starts_utc,
        now_utc,
    )

    assert len(merged_slots) == FORECAST_SLOT_COUNT
    assert [slot["load"] for slot in merged_slots[:historical_count]] == [
        float(index) for index in range(historical_count)
    ]
    assert merged_slots[historical_count]["load"] == pytest.approx(
        float(1000 + historical_count)
    )
    assert merged_slots[-1]["load"] == pytest.approx(float(1000 + FORECAST_SLOT_COUNT - 1))

    snapshot = coordinator.forecast_slots
    snapshot[0]["load"] = -1.0
    assert coordinator.forecast_slots[0]["load"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_coordinator_prunes_aged_rows(tmp_path: Path) -> None:
    """The daily retention job should prune stale rows from every table."""
    coordinator, _hass = _make_coordinator(tmp_path)
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    with (
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.Store",
            return_value=store,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_change",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.energyadvisor.coordinators.household_forecast_coordinator.async_track_time_interval",
            return_value=lambda: None,
        ),
    ):
        coordinator._store = store
        await coordinator.async_setup()

    db = coordinator._ensure_db()
    db.execute("DELETE FROM raw_samples")
    db.execute("DELETE FROM raw_events")
    db.execute("DELETE FROM interval_energy")
    db.execute("DELETE FROM slot_rows")
    db.execute("DELETE FROM forecast_runs")
    db.commit()
    now_utc = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    old_raw = now_utc - timedelta(days=22)
    recent_raw = now_utc - timedelta(days=1)
    old_slot = now_utc - timedelta(days=181)
    recent_slot = now_utc - timedelta(days=10)
    old_forecast = now_utc - timedelta(days=15)
    recent_forecast = now_utc - timedelta(days=2)

    db.executemany(
        "INSERT OR REPLACE INTO raw_samples (ts_utc, sensor_key, value, unit, quality) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (old_raw.isoformat(timespec="seconds"), "test_old", 1.0, "kWh", "ok"),
            (recent_raw.isoformat(timespec="seconds"), "test_recent", 2.0, "kWh", "ok"),
        ],
    )
    db.executemany(
        "INSERT OR REPLACE INTO raw_events (ts_utc, event_key, state_num, state_text, quality) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                old_raw.isoformat(timespec="seconds"),
                "test_old_event",
                1.0,
                "on",
                "ok",
            ),
            (
                recent_raw.isoformat(timespec="seconds"),
                "test_recent_event",
                0.0,
                "off",
                "ok",
            ),
        ],
    )
    db.execute(
        "INSERT OR REPLACE INTO interval_energy "
        "(ts_from_utc, ts_to_utc, sensor_key, delta_kwh, interval_sec, interval_kw, quality) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            old_raw.isoformat(timespec="seconds"),
            old_raw.isoformat(timespec="seconds"),
            "test_old_interval",
            1.0,
            60,
            60.0,
            "ok",
        ),
    )
    db.execute(
        "INSERT OR REPLACE INTO interval_energy "
        "(ts_from_utc, ts_to_utc, sensor_key, delta_kwh, interval_sec, interval_kw, quality) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            recent_raw.isoformat(timespec="seconds"),
            recent_raw.isoformat(timespec="seconds"),
            "test_recent_interval",
            2.0,
            60,
            120.0,
            "ok",
        ),
    )
    db.execute(
        "INSERT OR REPLACE INTO slot_rows "
        "(slot_start_utc, slot_energy_kwh, load_kw, sample_count, quality_score, features_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            old_slot.isoformat(timespec="seconds"),
            1.0,
            4.0,
            1,
            1.0,
            "{}",
        ),
    )
    db.execute(
        "INSERT OR REPLACE INTO slot_rows "
        "(slot_start_utc, slot_energy_kwh, load_kw, sample_count, quality_score, features_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            recent_slot.isoformat(timespec="seconds"),
            2.0,
            8.0,
            2,
            1.0,
            "{}",
        ),
    )
    db.execute(
        "INSERT OR REPLACE INTO forecast_runs "
        "(generated_at_utc, slot_start_utc, load_kw) VALUES (?, ?, ?)",
        (
            old_forecast.isoformat(timespec="seconds"),
            old_forecast.isoformat(timespec="seconds"),
            1.0,
        ),
    )
    db.execute(
        "INSERT OR REPLACE INTO forecast_runs "
        "(generated_at_utc, slot_start_utc, load_kw) VALUES (?, ?, ?)",
        (
            recent_forecast.isoformat(timespec="seconds"),
            recent_forecast.isoformat(timespec="seconds"),
            2.0,
        ),
    )
    db.commit()

    await coordinator._async_prune_raw_history(now_utc)

    with sqlite3.connect(coordinator._db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_samples").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM interval_energy").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM slot_rows").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM forecast_runs").fetchone()[0] == 1
        assert conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("last_raw_prune_utc",),
        ).fetchone()[0] == now_utc.isoformat(timespec="seconds")
