"""Tests for the household forecast coordinator."""

from __future__ import annotations

import asyncio
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
    METER_ENTITY_META_KEY,
    STATIC_LOAD_FORECAST_KW,
    STATIC_REASON,
    HouseholdForecastCoordinator,
    _WeightedStats,
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


_COORDINATOR_MODULE = (
    "custom_components.energyadvisor.coordinators.household_forecast_coordinator"
)


async def _setup_coordinator(coordinator: HouseholdForecastCoordinator) -> None:
    """Run async_setup with storage and listeners stubbed out."""
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    with (
        patch(f"{_COORDINATOR_MODULE}.Store", return_value=store),
        patch(
            f"{_COORDINATOR_MODULE}.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            f"{_COORDINATOR_MODULE}.async_track_time_change",
            return_value=lambda: None,
        ),
        patch(
            f"{_COORDINATOR_MODULE}.async_track_time_interval",
            return_value=lambda: None,
        ),
    ):
        coordinator._store = store
        await coordinator.async_setup()


async def _capture_meter(
    coordinator: HouseholdForecastCoordinator,
    state: str,
    unit: str,
    moment: datetime,
) -> None:
    meter_target = coordinator._capture_targets["sensor.household_meter"]
    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state=state,
            attributes={"unit_of_measurement": unit},
            last_updated=moment,
        ),
        source="event",
    )


def _insert_slot_row(
    db: sqlite3.Connection,
    slot_start: datetime,
    load_kw: float,
    *,
    quality: float = 1.0,
    gross_kwh: float | None = None,
) -> None:
    """Insert one slot row; ``gross_kwh`` mimics the stored gross energy feature."""
    local_start = dt_util.as_local(slot_start)
    features: dict[str, object] = {
        "slot_index": (local_start.hour * 60 + local_start.minute) // 15,
        "day_type": "workday",
        "is_holiday": False,
    }
    if gross_kwh is not None:
        features["gross_load_kwh"] = gross_kwh
    db.execute(
        "INSERT OR REPLACE INTO slot_rows "
        "(slot_start_utc, slot_energy_kwh, load_kw, sample_count, "
        "quality_score, features_json) VALUES (?, ?, ?, ?, ?, ?)",
        (
            slot_start.isoformat(timespec="seconds"),
            load_kw * 0.25,
            load_kw,
            1,
            quality,
            json.dumps(features, separators=(",", ":"), sort_keys=True),
        ),
    )


def _local_midnight_utc(moment: datetime) -> datetime:
    """Return the UTC time of local midnight on the local day of ``moment``."""
    local = dt_util.as_local(moment)
    return dt_util.as_utc(local.replace(hour=0, minute=0, second=0, microsecond=0))


@pytest.mark.asyncio
async def test_coordinator_creates_sqlite_schema_and_capture_targets(
    tmp_path: Path,
) -> None:
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

    assert len(coordinator._listeners) == 1
    assert len(coordinator._capture_listeners) == 2
    assert coordinator._capture_targets["sensor.household_meter"].key == (
        CONF_POWER_METER_CONSUMPTION
    )
    assert coordinator._capture_targets["binary_sensor.water_heater_active"].is_event
    assert not coordinator._capture_targets["sensor.battery_soc"].is_event
    assert coordinator._capture_targets[
        "sensor.household_meter"
    ].poll_interval == timedelta(seconds=60)
    assert coordinator._capture_targets[
        "sensor.battery_soc"
    ].poll_interval == timedelta(seconds=120)
    assert mock_state_change.call_count == 1
    assert mock_time_change.call_count == 1
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

    assert len(coordinator._listeners) == 1
    assert len(coordinator._capture_listeners) == 2
    assert coordinator._capture_targets["sensor.household_meter"].key == (
        CONF_POWER_METER_CONSUMPTION
    )
    assert mock_state_change.call_count == 1
    assert mock_time_change.call_count == 1
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
async def test_coordinator_finalizes_interval_energy_and_slot_rows(
    tmp_path: Path,
) -> None:
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
        # Load is the average over the observed part of the slot, not the slot.
        assert slot_rows[0][2] == pytest.approx(1.5)
        assert slot_rows[0][3] == 1
        assert slot_rows[0][4] == pytest.approx(0.25)
        assert json.loads(slot_rows[0][5])["required_missing"] is True
        assert slot_rows[1][1] == pytest.approx(0.0625)
        assert slot_rows[1][2] == pytest.approx(1.5)
        assert slot_rows[1][3] == 1
        assert slot_rows[1][4] == pytest.approx(1 / 12)
        assert json.loads(slot_rows[1][5])["required_missing"] is True

        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("last_finalized_slot_utc",),
            ).fetchone()[0]
            == "2026-09-05T12:15:00+00:00"
        )
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("last_generation_utc",),
            ).fetchone()[0]
            == "2026-09-05T12:30:00+00:00"
        )
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
        assert (
            conn.execute(
                "SELECT quality FROM interval_energy WHERE sensor_key = ?",
                (CONF_POWER_METER_CONSUMPTION,),
            ).fetchone()[0]
            == "sparse_gap"
        )

        slot_row = conn.execute(
            "SELECT slot_energy_kwh, load_kw, sample_count, quality_score, "
            "features_json FROM slot_rows ORDER BY slot_start_utc"
        ).fetchone()
        assert slot_row[0] == pytest.approx(1.0)
        assert slot_row[1] == pytest.approx(7.5)
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

    meter_target = coordinator._capture_targets["sensor.household_meter"]
    fresh_meter_sample = datetime(2026, 9, 9, 11, 59, 30, tzinfo=UTC)
    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state="12.500",
            attributes={"unit_of_measurement": "kWh"},
            last_updated=fresh_meter_sample,
        ),
        source="event",
    )

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
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM forecast_runs WHERE generated_at_utc = ?",
                (latest_generation,),
            ).fetchone()[0]
            == FORECAST_SLOT_COUNT
        )

    # The state follows the slot covering "now" (00:20 is the second slot), not
    # the midnight-anchored first slot of the forecast.
    second_slot_time = dt_util.as_local(
        datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    ).replace(hour=0, minute=20, second=0, microsecond=0)
    with patch(
        "custom_components.energyadvisor.coordinators.household_forecast_coordinator.dt_util.now",
        return_value=second_slot_time,
    ):
        assert coordinator.load_forecast_kw == pytest.approx(1.28)
        assert coordinator.household_load_forecast_w == pytest.approx(1280.0)
        assert coordinator.current_load_kw == pytest.approx(1.28)
    assert coordinator.learning_nights == 8
    assert coordinator.data_since == dt_util.as_local(base_day).date().isoformat()
    assert (
        coordinator.last_sample_date
        == dt_util.as_local(base_day + timedelta(days=7, minutes=45)).date().isoformat()
    )
    assert coordinator.last_sample_kw == pytest.approx(0.94)
    assert coordinator.quality_status == "ok"
    assert coordinator.quality_warnings == []
    assert coordinator.last_valid_required_sample == dt_util.as_local(
        fresh_meter_sample
    ).strftime("%Y-%m-%dT%H:%M")
    assert "seasonal baseline" in coordinator.reason
    assert coordinator.forecast_slots[0]["load"] == pytest.approx(0.72)
    assert coordinator.forecast_slots[1]["load"] == pytest.approx(1.28)
    assert (
        coordinator.forecast_slots[0]["load"] != coordinator.forecast_slots[1]["load"]
    )


@pytest.mark.asyncio
async def test_coordinator_marks_required_meter_outage_as_stale(
    tmp_path: Path,
) -> None:
    """A short required-meter outage should surface stale diagnostics."""
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
        patch.object(coordinator, "_slot_day_type", return_value="workday"),
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

    meter_target = coordinator._capture_targets["sensor.household_meter"]
    stale_meter_sample = datetime(2026, 9, 9, 11, 49, 30, tzinfo=UTC)
    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state="12.500",
            attributes={"unit_of_measurement": "kWh"},
            last_updated=stale_meter_sample,
        ),
        source="event",
    )

    assert await coordinator._async_finalize_history(
        datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    )

    assert coordinator.quality_status == "stale"
    assert "required_meter_stale" in coordinator.quality_warnings
    assert "stale" in coordinator.reason
    assert coordinator.last_valid_required_sample == dt_util.as_local(
        stale_meter_sample
    ).strftime("%Y-%m-%dT%H:%M")


@pytest.mark.asyncio
async def test_coordinator_records_invalid_sample_warning(tmp_path: Path) -> None:
    """Dropped samples should surface as a retained quality warning."""
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
        patch.object(coordinator, "_slot_day_type", return_value="workday"),
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

    meter_target = coordinator._capture_targets["sensor.household_meter"]
    fresh_meter_sample = datetime(2026, 9, 9, 11, 59, 30, tzinfo=UTC)
    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state="12.500",
            attributes={"unit_of_measurement": "kWh"},
            last_updated=fresh_meter_sample,
        ),
        source="event",
    )
    await coordinator._async_capture_target(
        meter_target,
        SimpleNamespace(
            entity_id=meter_target.entity_id,
            state="not-a-number",
            attributes={"unit_of_measurement": "kWh"},
            last_updated=fresh_meter_sample + timedelta(seconds=15),
        ),
        source="event",
    )

    assert await coordinator._async_finalize_history(
        datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    )

    assert coordinator.quality_status == "degraded"
    assert "invalid_sample_dropped" in coordinator.quality_warnings
    assert "invalid samples were dropped" in coordinator.reason
    assert coordinator.last_valid_required_sample == dt_util.as_local(
        fresh_meter_sample
    ).strftime("%Y-%m-%dT%H:%M")


def test_coordinator_applies_residual_correction_to_step_change(
    tmp_path: Path,
) -> None:
    """Recent sustained errors should bend the next few forecast slots upward."""
    coordinator, _hass = _make_coordinator(tmp_path)
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()

    now_utc = datetime(2026, 9, 8, 18, 0, tzinfo=UTC)
    local_now = dt_util.as_local(now_utc)
    current_slot_index = (local_now.hour * 60 + local_now.minute) // 15
    local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    base_day = local_day_start - timedelta(days=7)
    current_day = local_day_start
    step_start_slot = max(0, current_slot_index - 4)

    async def _exercise() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
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
            patch.object(coordinator, "_slot_day_type", return_value="workday"),
        ):
            coordinator._store = store
            await coordinator.async_setup()

        db = coordinator._ensure_db()

        def _insert_slot(day_start: datetime, slot_index: int, load_kw: float) -> None:
            slot_start = day_start + timedelta(minutes=15 * slot_index)
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

        for day_offset in range(8):
            day_start = base_day + timedelta(days=day_offset)
            for slot_index in range(current_slot_index):
                _insert_slot(day_start, slot_index, 0.6)
        db.commit()

        assert await coordinator._async_finalize_history(now_utc)

        for slot_index in range(step_start_slot, current_slot_index):
            _insert_slot(current_day, slot_index, 2.6)
        db.commit()

        with patch.object(
            coordinator, "_compute_residual_correction_kw_sync", return_value=0.0
        ):
            baseline_slots, _summary = (
                coordinator._build_forecast_slots_from_history_sync(now_utc)
            )

        assert await coordinator._async_finalize_history(now_utc)
        return baseline_slots, coordinator.forecast_slots

    baseline_slots, corrected_slots = asyncio.run(_exercise())
    baseline_future = [
        float(slot["load"])
        for slot in baseline_slots[current_slot_index : current_slot_index + 4]
    ]
    corrected_future = [
        float(slot["load"])
        for slot in corrected_slots[current_slot_index : current_slot_index + 4]
    ]
    actual_future_kw = 2.6
    baseline_mae = sum(abs(load - actual_future_kw) for load in baseline_future) / len(
        baseline_future
    )
    corrected_mae = sum(
        abs(load - actual_future_kw) for load in corrected_future
    ) / len(corrected_future)

    assert corrected_future[0] - baseline_future[0] <= 1.51
    assert corrected_mae <= baseline_mae * 0.8


def test_coordinator_keeps_historical_forecast_slots_stable_on_refresh(
    tmp_path: Path,
) -> None:
    """Refreshes should only rewrite the current and future forecast slots."""
    coordinator, _hass = _make_coordinator(tmp_path)
    now_utc = datetime(2026, 9, 7, 20, 0, tzinfo=UTC)
    slot_starts_utc = coordinator._forecast_slot_starts_utc(now_utc)
    current_slot_start_utc = coordinator._floor_to_slot_start_utc(now_utc)
    historical_count = sum(
        1
        for slot_start_utc in slot_starts_utc
        if slot_start_utc < current_slot_start_utc
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
    assert merged_slots[-1]["load"] == pytest.approx(
        float(1000 + FORECAST_SLOT_COUNT - 1)
    )

    snapshot = coordinator.forecast_slots
    snapshot[0]["load"] = -1.0
    assert coordinator.forecast_slots[0]["load"] == pytest.approx(0.0)


def test_coordinator_current_load_kw_uses_current_slot_not_first_slot(
    tmp_path: Path,
) -> None:
    """The sensor value should track the current slot, not the midnight anchor."""
    coordinator, _hass = _make_coordinator(tmp_path)
    now_utc = datetime(2026, 9, 7, 14, 32, tzinfo=UTC)
    slot_starts_utc = coordinator._forecast_slot_starts_utc(now_utc)

    coordinator._forecast_slots = [
        {
            "from": dt_util.as_local(slot_start_utc).strftime("%Y-%m-%dT%H:%M"),
            "load": float(index),
        }
        for index, slot_start_utc in enumerate(slot_starts_utc)
    ]

    with patch(
        "custom_components.energyadvisor.coordinators.household_forecast_coordinator.dt_util.now",
        return_value=dt_util.as_local(now_utc),
    ):
        current_index = coordinator._current_slot_index_local()
        current_value = coordinator.current_load_kw
        legacy_value = coordinator.load_forecast_kw
        legacy_watts = coordinator.household_load_forecast_w

    assert current_index != 0
    assert current_value == pytest.approx(float(current_index))
    # The attribute aliases follow the current slot too, not forecasts[0].
    assert legacy_value == pytest.approx(current_value)
    assert legacy_watts == pytest.approx(current_value * 1000.0)
    # forecasts[0] still reflects the frozen midnight slot, distinct from state.
    assert coordinator.forecast_slots[0]["load"] == pytest.approx(0.0)
    assert current_value != coordinator.forecast_slots[0]["load"]


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


def test_weighted_stats_trims_extremes_only_with_enough_samples() -> None:
    """A single glitch must not drag a bucket, but tiny buckets stay untrimmed."""
    stats = _WeightedStats()
    for value in [1.0] * 9 + [100.0]:
        stats.add(value, 1.0)
    assert stats.count == 10
    assert stats.mean() == pytest.approx(1.0)

    small = _WeightedStats()
    small.add(1.0, 1.0)
    small.add(100.0, 1.0)
    assert small.mean() == pytest.approx(50.5)

    empty = _WeightedStats()
    empty.add(float("nan"), 1.0)
    empty.add(5.0, 0.0)
    assert empty.count == 0
    assert empty.mean() is None


def test_unit_scale_distinguishes_milli_and_mega_units(tmp_path: Path) -> None:
    """Unit symbols are case sensitive: mW is a milliwatt, MW a megawatt."""
    coordinator, _hass = _make_coordinator(tmp_path)

    assert coordinator._unit_scale("mW") == pytest.approx(1e-6)
    assert coordinator._unit_scale("mWh") == pytest.approx(1e-6)
    assert coordinator._unit_scale("MW") == pytest.approx(1000.0)
    assert coordinator._unit_scale("MWh") == pytest.approx(1000.0)
    assert coordinator._unit_scale("W") == pytest.approx(0.001)
    assert coordinator._unit_scale("kWh") == pytest.approx(1.0)
    assert coordinator._value_to_kw(1_000_000.0, "mW") == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_coordinator_drops_implausible_counter_jump(tmp_path: Path) -> None:
    """A counter jump from swapping the meter source is not household energy."""
    coordinator, _hass = _make_coordinator(tmp_path)
    await _setup_coordinator(coordinator)

    first = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    await _capture_meter(coordinator, "16242.9", "kWh", first)
    await _capture_meter(coordinator, "24146.2", "kWh", first + timedelta(minutes=5))
    now = first + timedelta(minutes=15)

    assert await coordinator._async_finalize_history(now)

    with sqlite3.connect(coordinator._db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM interval_energy").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM slot_rows").fetchone()[0] == 0
    assert "invalid_sample_dropped" in coordinator._active_quality_warning_codes(now)


@pytest.mark.asyncio
async def test_coordinator_ignores_counter_jitter_but_flags_resets(
    tmp_path: Path,
) -> None:
    """Tiny counter decreases are noise; a large drop is a reset worth reporting."""
    coordinator, _hass = _make_coordinator(tmp_path)
    await _setup_coordinator(coordinator)

    first = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    now = first + timedelta(minutes=15)
    await _capture_meter(coordinator, "100.000", "kWh", first)
    await _capture_meter(coordinator, "99.990", "kWh", first + timedelta(minutes=5))

    assert await coordinator._async_finalize_history(now)
    assert "invalid_sample_dropped" not in coordinator._active_quality_warning_codes(
        now
    )

    await _capture_meter(coordinator, "2.000", "kWh", first + timedelta(minutes=10))

    assert await coordinator._async_finalize_history(now)
    assert "invalid_sample_dropped" in coordinator._active_quality_warning_codes(now)


@pytest.mark.asyncio
async def test_history_excludes_stalled_and_implausible_slots(tmp_path: Path) -> None:
    """Slots from a meter that was not counting must not train the forecast."""
    coordinator, _hass = _make_coordinator(tmp_path)
    await _setup_coordinator(coordinator)
    db = coordinator._ensure_db()
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    healthy_day = _local_midnight_utc(now - timedelta(days=5))
    catch_up_day = _local_midnight_utc(now - timedelta(days=4))
    mixed_day = _local_midnight_utc(now - timedelta(days=3))
    step = timedelta(minutes=15)

    for index in range(96):
        _insert_slot_row(db, healthy_day + step * index, 0.5, gross_kwh=0.125)
        # A stuck counter followed by a catch-up burst: mostly zero slots.
        if index < 80:
            _insert_slot_row(db, catch_up_day + step * index, 0.0, gross_kwh=0.0)
        else:
            _insert_slot_row(db, catch_up_day + step * index, 6.0, gross_kwh=1.5)
        if index < 4:
            _insert_slot_row(db, mixed_day + step * index, 0.0, gross_kwh=0.0)
        elif index == 4:
            _insert_slot_row(db, mixed_day + step * index, 31613.5, gross_kwh=7903.4)
        else:
            _insert_slot_row(db, mixed_day + step * index, 0.5, gross_kwh=0.125)
    db.commit()

    rows = coordinator._load_historical_slot_rows_sync(now)

    local_dates = {dt_util.as_local(row.slot_start_utc).date() for row in rows}
    assert local_dates == {
        dt_util.as_local(healthy_day).date(),
        dt_util.as_local(mixed_day).date(),
    }
    # Healthy day, plus the mixed day minus 4 stalled and 1 implausible slot.
    assert len(rows) == 96 + 91
    assert all(0.0 < row.load_kw <= 50.0 and not row.stalled for row in rows)


@pytest.mark.asyncio
async def test_forecast_ignores_counter_glitch_and_outlier_slots(
    tmp_path: Path,
) -> None:
    """A poisoned slot row must not leak into the published forecast."""
    coordinator, _hass = _make_coordinator(tmp_path)
    await _setup_coordinator(coordinator)
    db = coordinator._ensure_db()
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    first_day = _local_midnight_utc(now) - timedelta(days=8)
    step = timedelta(minutes=15)

    for day in range(8):
        for index in range(16):
            _insert_slot_row(db, first_day + timedelta(days=day) + step * index, 0.5)
    # A counter jump, and a spike that passes the hard cap but towers over history.
    _insert_slot_row(db, first_day + timedelta(days=2) + step, 31613.5)
    _insert_slot_row(db, first_day + timedelta(days=3) + step * 2, 20.0)
    db.commit()

    assert await coordinator._async_finalize_history(now)

    loads = [float(slot["load"]) for slot in coordinator.forecast_slots]
    assert max(loads) == pytest.approx(0.5, abs=0.01)


@pytest.mark.asyncio
async def test_forecast_trains_on_the_last_56_days_only(tmp_path: Path) -> None:
    """The training window is measured in days, not in rows."""
    coordinator, _hass = _make_coordinator(tmp_path)
    await _setup_coordinator(coordinator)
    db = coordinator._ensure_db()
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    midnight = _local_midnight_utc(now)
    step = timedelta(minutes=15)

    for day in range(1, 9):
        for index in range(4):
            _insert_slot_row(db, midnight - timedelta(days=day) + step * index, 1.0)
    for day in range(70, 100):
        for index in range(4):
            _insert_slot_row(db, midnight - timedelta(days=day) + step * index, 5.0)
    db.commit()

    assert await coordinator._async_finalize_history(now)

    assert float(coordinator.forecast_slots[0]["load"]) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_forecast_trend_is_anchored_at_now_and_fades(tmp_path: Path) -> None:
    """A recent step should lift the current slot and fade over the next hours."""
    coordinator, _hass = _make_coordinator(tmp_path)
    await _setup_coordinator(coordinator)
    db = coordinator._ensure_db()
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    midnight = _local_midnight_utc(now)
    local_now = dt_util.as_local(now)
    current_index = (local_now.hour * 60 + local_now.minute) // 15
    step = timedelta(minutes=15)

    for day in range(1, 9):
        for index in range(96):
            _insert_slot_row(db, midnight - timedelta(days=day) + step * index, 0.5)
    for index in range(current_index):
        load_kw = 1.5 if index >= current_index - 8 else 0.5
        _insert_slot_row(db, midnight + step * index, load_kw)
    db.commit()

    assert await coordinator._async_finalize_history(now)

    slots = [float(slot["load"]) for slot in coordinator.forecast_slots]
    # The same slot tomorrow is far enough ahead to show the plain baseline.
    lift_now = slots[current_index] - slots[current_index + 96]
    assert 0.25 < lift_now < 0.4
    assert slots[current_index + 1] < slots[current_index]
    assert slots[current_index + 24] < slots[current_index + 1]
    assert slots[current_index - 1] == pytest.approx(
        slots[current_index + 95], abs=0.02
    )


def test_residual_correction_compares_against_uncorrected_forecast(
    tmp_path: Path,
) -> None:
    """Residuals use the uncorrected forecast so a correction cannot cancel itself."""
    coordinator, _hass = _make_coordinator(tmp_path)
    db = coordinator._ensure_db()
    now = datetime(2026, 9, 9, 18, 0, tzinfo=UTC)
    current_slot = coordinator._floor_to_slot_start_utc(now)
    closed_slots = [current_slot - timedelta(minutes=15 * k) for k in (4, 3, 2, 1)]
    for slot_start in closed_slots:
        _insert_slot_row(db, slot_start, 2.6)
    db.commit()
    keys = [
        dt_util.as_local(slot_start).strftime("%Y-%m-%dT%H:%M")
        for slot_start in closed_slots
    ]
    coordinator._base_forecast_slots = [{"from": key, "load": 0.6} for key in keys]
    # The published forecast already contains the correction and nearly matches.
    coordinator._forecast_slots = [{"from": key, "load": 2.1} for key in keys]

    correction_kw = coordinator._compute_residual_correction_kw_sync(
        now, learning_nights=8
    )

    assert correction_kw == pytest.approx(1.5)

    coordinator._base_forecast_slots = []
    assert (
        coordinator._compute_residual_correction_kw_sync(now, learning_nights=8) == 0.0
    )


def test_sparse_sampling_warning_needs_clearly_low_quality(tmp_path: Path) -> None:
    """Slightly short coverage is normal; only clearly low quality warns."""
    coordinator, _hass = _make_coordinator(tmp_path)
    db = coordinator._ensure_db()
    now = datetime(2026, 9, 9, 18, 0, tzinfo=UTC)
    midnight = _local_midnight_utc(now)
    current_slot = coordinator._floor_to_slot_start_utc(now)
    step = timedelta(minutes=15)

    for day in range(1, 9):
        for index in range(4):
            _insert_slot_row(db, midnight - timedelta(days=day) + step * index, 0.5)
    for k in (4, 3, 2, 1):
        _insert_slot_row(db, current_slot - step * k, 0.5, quality=0.97)
    db.commit()

    summary = coordinator._build_forecast_summary(
        coordinator._load_historical_slot_rows_sync(now), now
    )
    assert "sparse_sampling" not in summary.quality_warnings

    _insert_slot_row(db, current_slot - step, 0.5, quality=0.5)
    db.commit()
    summary = coordinator._build_forecast_summary(
        coordinator._load_historical_slot_rows_sync(now), now
    )
    assert "sparse_sampling" in summary.quality_warnings


@pytest.mark.asyncio
async def test_coordinator_discards_meter_history_when_meter_entity_changes(
    tmp_path: Path,
) -> None:
    """A different meter entity has its own counter, so old meter data is dropped."""
    coordinator, _hass = _make_coordinator(tmp_path)
    await _setup_coordinator(coordinator)
    db = coordinator._ensure_db()
    assert (
        db.execute(
            "SELECT value FROM meta WHERE key = ?", (METER_ENTITY_META_KEY,)
        ).fetchone()[0]
        == "sensor.household_meter"
    )

    moment = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    ts = moment.isoformat(timespec="seconds")
    ts_end = (moment + timedelta(minutes=10)).isoformat(timespec="seconds")
    db.executemany(
        "INSERT OR REPLACE INTO raw_samples "
        "(ts_utc, sensor_key, value, unit, quality) VALUES (?, ?, ?, ?, ?)",
        [
            (ts, CONF_POWER_METER_CONSUMPTION, 100.0, "kWh", "ok"),
            (ts, CONF_WATER_HEATER_POWER_ENTITY, 500.0, "W", "ok"),
        ],
    )
    db.execute(
        "INSERT OR REPLACE INTO raw_events "
        "(ts_utc, event_key, state_num, state_text, quality) VALUES (?, ?, ?, ?, ?)",
        (ts, CONF_WATER_HEATER_ACTIVE_ENTITY, 1.0, "on", "ok"),
    )
    db.execute(
        "INSERT OR REPLACE INTO interval_energy "
        "(ts_from_utc, ts_to_utc, sensor_key, delta_kwh, interval_sec, "
        "interval_kw, quality) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, ts_end, CONF_POWER_METER_CONSUMPTION, 1.0, 600, 6.0, "ok"),
    )
    _insert_slot_row(db, moment, 1.0)
    db.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        ("last_finalized_slot_utc", ts),
    )
    db.commit()
    await coordinator.async_shutdown()

    same_meter, _hass = _make_coordinator(tmp_path)
    await _setup_coordinator(same_meter)
    assert (
        same_meter._ensure_db().execute("SELECT COUNT(*) FROM slot_rows").fetchone()[0]
        == 1
    )
    await same_meter.async_shutdown()

    swapped, _hass = _make_coordinator(
        tmp_path, extra_options={CONF_POWER_METER_CONSUMPTION: "sensor.other_meter"}
    )
    await _setup_coordinator(swapped)

    with sqlite3.connect(swapped._db_path()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM slot_rows").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM interval_energy").fetchone()[0] == 0
        assert [
            row[0] for row in conn.execute("SELECT sensor_key FROM raw_samples")
        ] == [CONF_WATER_HEATER_POWER_ENTITY]
        assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM meta WHERE key = ?",
                ("last_finalized_slot_utc",),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key = ?", (METER_ENTITY_META_KEY,)
            ).fetchone()[0]
            == "sensor.other_meter"
        )


@pytest.mark.asyncio
async def test_coordinator_keeps_history_when_meter_entity_was_never_recorded(
    tmp_path: Path,
) -> None:
    """History stored before the meter entity was tracked is kept, not purged."""
    coordinator, _hass = _make_coordinator(tmp_path)
    await _setup_coordinator(coordinator)
    db = coordinator._ensure_db()
    _insert_slot_row(db, datetime(2026, 9, 5, 12, 0, tzinfo=UTC), 1.0)
    db.execute("DELETE FROM meta WHERE key = ?", (METER_ENTITY_META_KEY,))
    db.commit()
    await coordinator.async_shutdown()

    restarted, _hass = _make_coordinator(tmp_path)
    await _setup_coordinator(restarted)

    db = restarted._ensure_db()
    assert db.execute("SELECT COUNT(*) FROM slot_rows").fetchone()[0] == 1
    assert (
        db.execute(
            "SELECT value FROM meta WHERE key = ?", (METER_ENTITY_META_KEY,)
        ).fetchone()[0]
        == "sensor.household_meter"
    )
