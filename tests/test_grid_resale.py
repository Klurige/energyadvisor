"""Accounting, persistence and HA boundaries for the grid resale ledger."""

import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from homeassistant.core import State
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energyadvisor.config_flow import EnergyAdvisorOptionFlowHandler
from custom_components.energyadvisor.const import DOMAIN
from custom_components.energyadvisor.coordinators.grid_resale_coordinator import (
    GridResaleCoordinator,
)
from custom_components.energyadvisor.grid_resale import (
    CONF_RESALE_ENABLED,
    POWER_KEYS,
    ResaleLedger,
    STOCKHOLM,
    flows,
    migrate_resale_options,
    power_kw,
)
from custom_components.energyadvisor.grid_resale_http import GridResaleExportView
from custom_components.energyadvisor.sensor.gridresalesensor import GridResaleSensor
from custom_components.energyadvisor.services import async_setup_resale_services

BASE = datetime(2025, 6, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def values(battery=0.0, imported=0.0, exported=0.0, solar=0.0):
    return {
        "battery": battery,
        "grid_import": imported,
        "grid_export": exported,
        "solar": solar,
    }


@pytest.fixture
def ledger(tmp_path):
    instance = ResaleLedger(tmp_path / "ledger.sqlite3")
    instance.initialize()
    instance.configuration({"entities": {"battery": "sensor.battery"}})
    return instance


def add(ledger, hour, readings, start=BASE, quality="measured"):
    return ledger.record(
        start + hour * HOUR,
        start + (hour + 1) * HOUR,
        readings,
        readings or {},
        1,
        quality=quality,
    )


def test_household_use_and_solar_replenishment_do_not_consume_allowance(ledger):
    add(ledger, 0, values(battery=1, imported=2))
    add(ledger, 1, values(battery=-1))
    add(ledger, 2, values(battery=1, solar=1))
    add(ledger, 3, values(battery=-1, exported=1))
    summary = ledger.summary(BASE)
    assert summary["grid_charged_kwh"] == 1
    assert summary["eligible_export_kwh"] == 1
    assert summary["allowance_kwh"] == 0


def test_two_daily_cycles_cap_exports_and_do_not_credit_household_import(ledger):
    add(ledger, 0, values(battery=1, imported=2))
    add(ledger, 1, values(battery=-2, exported=2))
    add(ledger, 2, values(imported=3))
    add(ledger, 3, values(battery=1, imported=1))
    add(ledger, 4, values(battery=-1, exported=1))
    add(ledger, 5, values(battery=-1, exported=1))
    assert ledger.summary(BASE)["eligible_export_kwh"] == 2
    assert ledger.summary(BASE)["allowance_kwh"] == 0


def test_solar_cannot_create_allowance_or_count_as_direct_export(ledger):
    add(ledger, 0, values(battery=2, imported=1, solar=2))
    add(ledger, 1, values(battery=-1, exported=1))
    assert ledger.summary(BASE)["eligible_export_kwh"] == 0
    add(ledger, 2, values(battery=1, imported=1))
    add(ledger, 3, values(battery=-1, exported=2, solar=2))
    assert ledger.summary(BASE)["allowance_kwh"] == 1
    assert flows(values(battery=-3, exported=3, solar=1), 1) == (0, 2)


def test_swedish_year_boundary_expires_allowance_and_splits_interval(ledger):
    midnight = datetime(2026, 1, 1, tzinfo=STOCKHOLM).astimezone(timezone.utc)
    ledger.record(
        midnight - HOUR * 2, midnight - HOUR, values(battery=2, imported=2), {}, 1
    )
    ledger.record(
        midnight - HOUR, midnight + HOUR, values(battery=-1, exported=1), {}, 1
    )
    assert ledger.summary(midnight - timedelta(seconds=1))["eligible_export_kwh"] == 1
    assert ledger.summary(midnight)["eligible_export_kwh"] == 0
    assert ledger.summary(midnight)["allowance_kwh"] == 0
    with sqlite3.connect(ledger.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM intervals").fetchone()[0] == 3


def test_charge_crossing_new_year_is_split_between_allowances(ledger):
    midnight = datetime(2026, 1, 1, tzinfo=STOCKHOLM).astimezone(timezone.utc)
    ledger.record(
        midnight - HOUR, midnight + HOUR, values(battery=1, imported=1), {}, 1
    )
    ledger.record(
        midnight + HOUR, midnight + HOUR * 2, values(battery=-2, exported=2), {}, 1
    )
    assert ledger.summary(midnight)["eligible_export_kwh"] == 1


def test_earlier_export_is_not_retroactively_qualified_by_later_charge(ledger):
    add(ledger, 0, values(battery=-1, exported=1))
    add(ledger, 1, values(battery=1, imported=1))
    assert ledger.summary(BASE)["eligible_export_kwh"] == 0
    assert ledger.summary(BASE)["allowance_kwh"] == 1


def test_historical_replay_is_idempotent_and_updates_later_exports(ledger):
    add(ledger, 3, values(battery=-1, exported=1))
    add(ledger, 4, values(battery=-1, exported=1))
    rows = [
        (
            BASE,
            BASE + HOUR,
            values(battery=2, imported=2),
            {"mean": 2},
            "hourly_estimate",
        ),
        (BASE + HOUR, BASE + HOUR * 2, values(battery=-1), {}, "hourly_estimate"),
    ]
    assert ledger.record_many(rows, 1, "history_hourly_estimate") == (2, 0)
    assert ledger.summary(BASE)["eligible_export_kwh"] == 2
    assert ledger.summary(BASE)["allowance_kwh"] == 0
    assert ledger.record_many(rows, 1, "history_hourly_estimate") == (0, 2)
    assert ledger.summary(BASE)["interval_count"] == 4
    reopened = ResaleLedger(ledger.path)
    reopened.initialize()
    assert reopened.summary(BASE) == ledger.summary(BASE)


def test_unsorted_history_chunk_replays_once_and_keeps_estimates_visible(ledger):
    rows = [
        (BASE + HOUR, BASE + HOUR * 2, values(battery=-1, exported=1), {}, "measured"),
        (BASE, BASE + HOUR, values(battery=1, imported=1), {}, "hourly_estimate"),
    ]
    assert ledger.record_many(rows, 1, "history_hourly_estimate") == (2, 0)
    summary = ledger.summary(BASE)
    assert summary["eligible_export_kwh"] == 1
    assert summary["estimated_interval_count"] == 1
    assert summary["allowance_kwh"] == 0


def test_partial_overlap_never_double_counts_live_intervals(ledger):
    add(ledger, 0, values(battery=1, imported=1))
    assert (
        ledger.record(
            BASE + HOUR / 2, BASE + HOUR * 2, values(battery=10, imported=10), {}, 1
        )
        == 0
    )
    assert ledger.summary(BASE)["grid_charged_kwh"] == 1


def test_invalid_readings_and_downtime_never_create_energy(ledger):
    add(ledger, 0, None, quality="missing_or_stale")
    ledger.note_gap(BASE + HOUR, BASE + HOUR * 2, 1)
    add(ledger, 2, values(battery=-1, exported=1))
    assert ledger.summary(BASE)["eligible_export_kwh"] == 0
    assert add(ledger, 1, values(battery=1, imported=1)) == 1
    assert ledger.summary(BASE)["eligible_export_kwh"] == 1
    for bad in (float("nan"), float("inf"), -1):
        with pytest.raises(ValueError):
            add(ledger, 4, values(imported=bad))


def test_complete_csv_has_source_evidence_settings_imports_and_years(ledger, tmp_path):
    add(ledger, 0, values(battery=1, imported=1))
    add(ledger, 1, values(battery=-1, exported=1), quality="hourly_estimate")
    ledger.log_import(BASE, BASE + HOUR * 2, "hourly_estimate", 2, 0)
    path = ledger.export_csv(tmp_path / "exports")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert {row["record_type"] for row in rows} == {
        "configurations",
        "samples",
        "intervals",
        "year_totals",
        "imports",
    }
    assert (
        sum(
            float(row["eligible_kwh"])
            for row in rows
            if row["record_type"] == "intervals"
        )
        == 1
    )
    assert (
        json.loads(
            next(
                row["evidence_json"] for row in rows if row["record_type"] == "samples"
            )
        )["battery"]
        == 1
    )
    add(ledger, 2, values(battery=1, imported=1))
    with path.open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.DictReader(stream)) == rows


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        ("1000", "W", 1),
        ("-2", "kW", -2),
        ("unavailable", "W", None),
        ("nan", "W", None),
        ("inf", "kW", None),
        ("1", "kWh", None),
    ],
)
def test_power_validation(value, unit, expected):
    assert power_kw(value, unit) == expected


def make_coordinator(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_RESALE_ENABLED: True,
            **{key: f"sensor.resale_{role}" for role, key in POWER_KEYS.items()},
        },
    )
    entry.add_to_hass(hass)
    return entry, GridResaleCoordinator(hass, entry)


def snapshot(at, readings):
    return {
        key: {"state": value, "unit": "kW", "reported_utc": at.isoformat()}
        for key, value in readings.items()
    }


async def test_live_capture_uses_signed_shared_power_and_rejects_stale_readings(
    hass, tmp_path
):
    entry, coordinator = make_coordinator(hass, tmp_path)
    assert (
        coordinator.entities["battery"] == entry.options["battery_charge_power_entity"]
    )
    await hass.async_add_executor_job(coordinator.ledger.initialize)
    coordinator._configuration_id = await hass.async_add_executor_job(
        coordinator.ledger.configuration, {}
    )
    await coordinator._async_capture(
        BASE, snapshot(BASE, values(battery=1, imported=1))
    )
    stop = BASE + timedelta(seconds=60)
    await coordinator._async_capture(
        stop, snapshot(stop, values(battery=-1, exported=1))
    )
    stop2 = stop + timedelta(seconds=60)
    await coordinator._async_capture(
        stop2, snapshot(stop2, values(battery=1, imported=1))
    )
    assert coordinator.data["eligible_export_kwh"] == pytest.approx(1 / 60)
    await coordinator._async_capture(
        stop2 + HOUR, snapshot(stop2 + HOUR, values(battery=1, imported=1))
    )
    assert coordinator.data["grid_charged_kwh"] == pytest.approx(1 / 60)
    assert coordinator.data["quality_status"] == "missing_or_stale"
    sensor = GridResaleSensor(entry, {}, coordinator)
    assert sensor.native_value == pytest.approx(1 / 60)
    assert sensor.last_reset == datetime(2025, 1, 1, tzinfo=STOCKHOLM)
    await coordinator.async_shutdown()


async def test_capture_database_failure_marks_sensor_unavailable(hass, tmp_path):
    _, coordinator = make_coordinator(hass, tmp_path)
    coordinator.ledger.summary = MagicMock(
        side_effect=sqlite3.OperationalError("disk full")
    )
    await coordinator._async_capture_event(BASE, snapshot(BASE, values()))
    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, sqlite3.OperationalError)


async def test_raw_history_uses_actual_power_and_does_not_bridge_missing_data(
    hass, tmp_path
):
    _, coordinator = make_coordinator(hass, tmp_path)
    history = {
        entity: [
            State(
                entity,
                str(reading * 1000),
                {"unit_of_measurement": "W"},
                last_changed=BASE,
                last_updated=BASE,
            )
        ]
        for role, entity in coordinator.entities.items()
        for reading in [values(battery=1, imported=1)[role]]
    }
    with patch(
        "homeassistant.components.recorder.history.get_significant_states",
        return_value=history,
    ):
        rows = coordinator._load_raw(BASE, BASE + timedelta(seconds=60))
        assert len(rows) == 1
        assert rows[0][2]["battery"] == 1
        assert coordinator._load_raw(BASE, BASE + HOUR) == []


async def test_hourly_import_validates_units_and_preserves_estimate_provenance(
    hass, tmp_path
):
    _, coordinator = make_coordinator(hass, tmp_path)
    metadata = {
        entity: (i, {"has_mean": True, "unit_of_measurement": "W"})
        for i, entity in enumerate(coordinator.entities.values())
    }
    statistics = {
        entity: [
            {
                "start": BASE.timestamp(),
                "mean": values(battery=1000, imported=1000)[role],
            }
        ]
        for role, entity in coordinator.entities.items()
    }
    with (
        patch(
            "homeassistant.components.recorder.statistics.get_metadata",
            return_value=metadata,
        ),
        patch(
            "homeassistant.components.recorder.statistics.statistics_during_period",
            return_value=statistics,
        ),
    ):
        rows = coordinator._load_hourly(BASE, BASE + HOUR)
        assert len(rows) == 1
        assert rows[0][2]["battery"] == 1
        assert rows[0][4] == "hourly_estimate"
        statistics[coordinator.entities["solar"]] = []
        assert coordinator._load_hourly(BASE, BASE + HOUR)[0][2] is None
        metadata[coordinator.entities["solar"]][1]["unit_of_measurement"] = "kWh"
        with pytest.raises(ValueError, match="unit"):
            coordinator._load_hourly(BASE, BASE + HOUR)


async def test_history_action_rejects_future_naive_and_unaligned_timestamps(
    hass, tmp_path
):
    _, coordinator = make_coordinator(hass, tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        await coordinator.async_import_history(
            BASE.replace(tzinfo=None), BASE + HOUR, "raw"
        )
    with pytest.raises(ValueError, match="future"):
        await coordinator.async_import_history(
            BASE, datetime.now(timezone.utc) + HOUR, "raw"
        )
    with pytest.raises(ValueError, match="whole UTC hour"):
        await coordinator.async_import_history(
            BASE + timedelta(minutes=1), BASE + HOUR, "hourly_estimate"
        )


async def test_history_orchestration_commits_chunks_and_skips_duplicate_intervals(
    hass, tmp_path
):
    _, coordinator = make_coordinator(hass, tmp_path)
    await hass.async_add_executor_job(coordinator.ledger.initialize)
    coordinator._configuration_id = await hass.async_add_executor_job(
        coordinator.ledger.configuration, {}
    )
    coordinator._load_raw = MagicMock(
        return_value=[
            (
                BASE,
                BASE + HOUR,
                values(battery=1, imported=1),
                {},
                "raw_reconstruction",
            ),
            (
                BASE + HOUR,
                BASE + HOUR * 2,
                values(battery=-1, exported=1),
                {},
                "raw_reconstruction",
            ),
        ]
    )
    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=lambda fn, *args: fn(*args)),
    )
    with patch("homeassistant.components.recorder.get_instance", return_value=recorder):
        response = await coordinator.async_import_history(BASE, BASE + HOUR * 2, "raw")
        assert response["inserted_intervals"] == 2
        response = await coordinator.async_import_history(BASE, BASE + HOUR * 2, "raw")
        assert response["inserted_intervals"] == 0
        assert response["overlapping_intervals_skipped"] == 2
    summary = await hass.async_add_executor_job(coordinator.ledger.summary, BASE)
    assert summary["eligible_export_kwh"] == 1
    await coordinator.async_shutdown()


async def test_setup_restart_and_new_year_keep_database_but_expire_allowance(
    hass, tmp_path
):
    from custom_components.energyadvisor import async_unload_entry
    from custom_components.energyadvisor.models import EnergyAdvisorRuntimeData

    entry, coordinator = make_coordinator(hass, tmp_path)
    now = datetime.now(timezone.utc)
    for role, entity in coordinator.entities.items():
        hass.states.async_set(entity, "0", {"unit_of_measurement": "kW"})
    await coordinator.async_setup()
    before = now - HOUR * 2
    await hass.async_add_executor_job(
        coordinator.ledger.record,
        before,
        before + HOUR,
        values(battery=1, imported=1),
        {},
        coordinator._configuration_id,
    )
    assert coordinator._unsubscribe_year_reset is not None
    await coordinator.async_shutdown()
    restored = GridResaleCoordinator(hass, entry)
    await restored.async_setup()
    assert restored.data["allowance_kwh"] == 1
    assert restored.data["recorded_restart_gaps"] == 1
    year = now.astimezone(STOCKHOLM).year
    boundary = datetime(year + 1, 1, 1, tzinfo=STOCKHOLM).astimezone(timezone.utc)
    await restored._async_new_year(boundary)
    assert restored.data["tax_year"] == year + 1
    assert restored.data["eligible_export_kwh"] == 0
    assert restored.data["allowance_kwh"] == 0
    hass.data[DOMAIN] = {
        entry.entry_id: EnergyAdvisorRuntimeData(
            levels_sensor=MagicMock(),
            compact_sensor=MagicMock(),
            coordinator=MagicMock(),
            grid_resale_coordinator=restored,
        )
    }
    with patch.object(
        hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=True)
    ):
        assert await async_unload_entry(hass, entry)
    assert restored._unsubscribe is None
    assert restored.ledger.path.exists()
    data = await hass.async_add_executor_job(restored.ledger.summary, now)
    assert data["allowance_kwh"] == 1


async def test_resale_options_only_enable_and_validate_existing_shared_sources(hass):
    handler = EnergyAdvisorOptionFlowHandler()
    handler.hass = hass
    handler._initialized = True
    selected = {key: f"sensor.resale_{role}" for role, key in POWER_KEYS.items()}
    for entity in selected.values():
        hass.states.async_set(entity, "0", {"unit_of_measurement": "W"})
    result = await handler.async_step_grid_resale({CONF_RESALE_ENABLED: True})
    assert result["errors"]["base"] == "resale_sources_invalid"
    assert {field.schema for field in result["data_schema"].schema} == {
        CONF_RESALE_ENABLED
    }
    handler.current_options.update(selected)
    hass.states.async_set(
        selected[POWER_KEYS["solar"]], "0", {"unit_of_measurement": "kWh"}
    )
    result = await handler.async_step_grid_resale({CONF_RESALE_ENABLED: True})
    assert result["errors"]["base"] == "resale_sources_invalid"
    hass.states.async_set(
        selected[POWER_KEYS["solar"]], "0", {"unit_of_measurement": "W"}
    )
    result = await handler.async_step_grid_resale({CONF_RESALE_ENABLED: True})
    assert result["type"] == "menu"
    assert handler.current_options[CONF_RESALE_ENABLED] is True
    assert (
        handler.current_options[POWER_KEYS["battery"]]
        == selected[POWER_KEYS["battery"]]
    )
    saved = await handler.async_step_done()
    assert saved["type"] == "create_entry"
    hass.states.async_set(
        selected[POWER_KEYS["solar"]], "0", {"unit_of_measurement": "kWh"}
    )
    assert (await handler.async_step_done())["errors"][
        "base"
    ] == "resale_sources_invalid"
    await handler.async_step_grid_resale({CONF_RESALE_ENABLED: False})
    assert all(
        handler.current_options[key] == entity for key, entity in selected.items()
    )


def test_legacy_sources_migrate_without_changing_shared_configuration():
    legacy = {
        "resale_grid_import_entity": "sensor.import",
        "resale_grid_export_entity": "sensor.export",
        "resale_battery_power_entity": "sensor.batteries_charge_discharge_power",
        "resale_solar_power_entity": "sensor.pv",
        "grid_resale_positive_charge": True,
        CONF_RESALE_ENABLED: True,
    }
    migrated, review = migrate_resale_options(legacy)
    assert not review
    assert migrated[POWER_KEYS["battery"]] == "sensor.batteries_charge_discharge_power"
    assert {key for key in migrated} == {*POWER_KEYS.values(), CONF_RESALE_ENABLED}
    assert migrate_resale_options(migrated) == (migrated, False)
    legacy[POWER_KEYS["battery"]] = "sensor.shared_battery"
    migrated, review = migrate_resale_options(legacy)
    assert review
    assert migrated[POWER_KEYS["battery"]] == "sensor.shared_battery"
    assert migrated[CONF_RESALE_ENABLED] is False
    legacy.pop(POWER_KEYS["battery"])
    legacy["grid_resale_positive_charge"] = False
    migrated, review = migrate_resale_options(legacy)
    assert review
    assert migrated[CONF_RESALE_ENABLED] is False


async def test_conflicting_legacy_sources_raise_repair_without_touching_ledger(hass):
    from homeassistant.helpers import issue_registry as ir
    from custom_components.energyadvisor import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_RESALE_ENABLED: True,
            POWER_KEYS["battery"]: "sensor.batteries_charge_discharge_power",
            "resale_battery_power_entity": "sensor.old_charge_only",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.energyadvisor._async_migrate_entity_registry",
            new=AsyncMock(),
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
        ),
    ):
        assert await async_setup_entry(hass, entry)
    assert not entry.options[CONF_RESALE_ENABLED]
    assert (
        entry.options[POWER_KEYS["battery"]]
        == "sensor.batteries_charge_discharge_power"
    )
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"grid_resale_shared_sources_{entry.entry_id}"
        )
        is not None
    )


async def test_actual_solar_source_can_be_configured_without_forecast(hass):
    handler = EnergyAdvisorOptionFlowHandler()
    handler.hass = hass
    handler._initialized = True
    hass.states.async_set("sensor.pv", "500", {"unit_of_measurement": "W"})
    result = await handler.async_step_solar_forecast({POWER_KEYS["solar"]: "sensor.pv"})
    assert result["type"] == "menu"
    assert handler.current_options[POWER_KEYS["solar"]] == "sensor.pv"


async def test_export_and_history_services_resolve_entries_and_return_responses(hass):
    coordinator = SimpleNamespace(
        async_export=AsyncMock(return_value={"download_url": "/api/private.csv"}),
        async_import_history=AsyncMock(return_value={"inserted_intervals": 2}),
    )
    hass.data[DOMAIN] = {"one": SimpleNamespace(grid_resale_coordinator=coordinator)}
    async_setup_resale_services(hass)
    result = await hass.services.async_call(
        DOMAIN, "export_grid_resale", {}, blocking=True, return_response=True
    )
    assert result["download_url"] == "/api/private.csv"
    result = await hass.services.async_call(
        DOMAIN,
        "import_grid_resale_history",
        {
            "start": BASE.isoformat(),
            "end": (BASE + HOUR).isoformat(),
        },
        blocking=True,
        return_response=True,
    )
    assert result["inserted_intervals"] == 2
    hass.data[DOMAIN]["two"] = SimpleNamespace(grid_resale_coordinator=coordinator)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "export_grid_resale", {}, blocking=True, return_response=True
        )


async def test_private_export_view_rejects_path_traversal(hass):
    view = GridResaleExportView(hass)
    with pytest.raises(web.HTTPNotFound):
        await view.get(MagicMock(), "../../token.txt")


async def test_export_download_requires_authentication(
    hass, hass_client, hass_client_no_auth, tmp_path, monkeypatch
):
    from aiohttp.resolver import ThreadedResolver
    from homeassistant.setup import async_setup_component

    monkeypatch.setattr("aiohttp.connector.DefaultResolver", ThreadedResolver)
    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(GridResaleExportView(hass))
    directory = tmp_path / "energyadvisor" / "exports"
    directory.mkdir(parents=True)
    filename = "a" * 32 + ".csv"
    (directory / filename).write_text("record_type,tax_year\nintervals,2025\n")
    url = "/api/energyadvisor/grid_resale/" + filename
    client = await hass_client_no_auth()
    response = await client.get(url)
    assert response.status == 401
    client = await hass_client()
    response = await client.get(url)
    assert response.status == 200
    assert "intervals,2025" in await response.text()
