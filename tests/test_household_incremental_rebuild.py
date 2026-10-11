"""Incremental history rebuild of the household forecast coordinator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.energyadvisor.const import (
    CONF_POWER_METER_CONSUMPTION,
    CONF_WATER_HEATER_ACTIVE_ENTITY,
    CONF_WATER_HEATER_POWER_ENTITY,
    CONF_WATER_HEATER_POWER_W,
)
from custom_components.energyadvisor.coordinators.household_forecast_coordinator import (
    HouseholdForecastCoordinator,
)

from .test_household_forecast_coordinator import _make_coordinator

UTC = timezone.utc
START = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=5)
# Samples sit off the slot grid so intervals straddle slot boundaries.
OFFSET = timedelta(seconds=150)
METER = CONF_POWER_METER_CONSUMPTION


def _subdir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def _ts(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _seed_meter(
    coordinator: HouseholdForecastCoordinator, first: datetime, last: datetime
) -> None:
    """Insert one W sample every five minutes with a varying load."""
    rows = []
    moment = first + OFFSET
    while moment <= last:
        index = int((moment - START - OFFSET) / STEP)
        rows.append((_ts(moment), METER, 500.0 + (index * 37) % 400, "W", "ok"))
        moment += STEP
    db = coordinator._ensure_db()
    db.executemany(
        "INSERT OR REPLACE INTO raw_samples "
        "(ts_utc, sensor_key, value, unit, quality) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()


def _slot_rows(coordinator: HouseholdForecastCoordinator) -> list[tuple]:
    return (
        coordinator._ensure_db()
        .execute(
            "SELECT slot_start_utc, slot_energy_kwh, load_kw, sample_count, "
            "quality_score, features_json FROM slot_rows ORDER BY slot_start_utc"
        )
        .fetchall()
    )


def _assert_same_slot_rows(left: list[tuple], right: list[tuple]) -> None:
    assert [row[0] for row in left] == [row[0] for row in right]
    for a, b in zip(left, right):
        assert a[1:5] == pytest.approx(b[1:5])
        assert a[5] == b[5]


def _record_sample_counts(coordinator: HouseholdForecastCoordinator) -> list[list]:
    """Spy on the samples handed to the interval builder."""
    seen: list[list] = []
    original = coordinator._build_interval_energy_rows

    def spy(samples):
        # Appliance sources without samples are also run through the builder.
        if samples:
            seen.append(list(samples))
        return original(samples)

    coordinator._build_interval_energy_rows = spy
    return seen


def test_incremental_rebuild_matches_a_full_rebuild(tmp_path: Path) -> None:
    first_now = START + timedelta(days=2, hours=12)
    second_now = START + timedelta(days=2, hours=15)

    incremental, _ = _make_coordinator(_subdir(tmp_path, "incremental"))
    _seed_meter(incremental, START, first_now)
    incremental._rebuild_history_sync(first_now)
    _seed_meter(incremental, first_now, second_now)
    incremental._rebuild_history_sync(second_now)

    full, _ = _make_coordinator(_subdir(tmp_path, "full"))
    _seed_meter(full, START, second_now)
    full._rebuild_history_sync(second_now)

    assert len(_slot_rows(full)) > 200
    _assert_same_slot_rows(_slot_rows(incremental), _slot_rows(full))
    assert incremental._last_finalized_slot_utc == full._last_finalized_slot_utc


def test_incremental_rebuild_reloads_only_the_recent_window(tmp_path: Path) -> None:
    first_now = START + timedelta(days=2, hours=12)
    second_now = START + timedelta(days=2, hours=15)
    coordinator, _ = _make_coordinator(tmp_path)
    seen = _record_sample_counts(coordinator)

    _seed_meter(coordinator, START, first_now)
    coordinator._rebuild_history_sync(first_now)
    _seed_meter(coordinator, first_now, second_now)
    coordinator._rebuild_history_sync(second_now)

    full_load, incremental_load = seen
    assert len(full_load) == int((first_now - START - OFFSET) / STEP) + 1
    # Last finalized slot is 11:45; the window starts two hours earlier, at
    # 09:45, with the single sample before it as an anchor.
    window_start = datetime(2026, 9, 5, 9, 45, tzinfo=UTC)
    assert incremental_load[0].ts_utc == window_start - STEP + OFFSET
    assert incremental_load[1].ts_utc == window_start + OFFSET
    assert incremental_load[-1].ts_utc == second_now - STEP + OFFSET
    assert len(incremental_load) == int((second_now - window_start) / STEP) + 1


def test_incremental_rebuild_keeps_slot_rows_older_than_the_window(
    tmp_path: Path,
) -> None:
    now = START + timedelta(days=2, hours=12)
    coordinator, _ = _make_coordinator(tmp_path)
    _seed_meter(coordinator, START, now)
    coordinator._rebuild_history_sync(now)

    old_slot = _ts(START + timedelta(days=1, hours=6))
    db = coordinator._ensure_db()
    db.execute(
        "UPDATE slot_rows SET load_kw = 99.0 WHERE slot_start_utc = ?", (old_slot,)
    )
    db.commit()
    later = now + timedelta(minutes=15)
    _seed_meter(coordinator, now, later)
    coordinator._rebuild_history_sync(later)

    assert (
        db.execute(
            "SELECT load_kw FROM slot_rows WHERE slot_start_utc = ?", (old_slot,)
        ).fetchone()[0]
        == 99.0
    )


def test_incremental_rebuild_does_not_shrink_when_the_window_has_no_samples(
    tmp_path: Path,
) -> None:
    now = START + timedelta(days=2, hours=12)
    coordinator, _ = _make_coordinator(tmp_path)
    _seed_meter(coordinator, START, now)
    coordinator._rebuild_history_sync(now)
    before = _slot_rows(coordinator)

    coordinator._rebuild_history_sync(now + timedelta(minutes=15))

    assert _slot_rows(coordinator)[: len(before) - 8] == before[: len(before) - 8]


def test_first_rebuild_after_startup_is_always_full(tmp_path: Path) -> None:
    now = START + timedelta(days=2, hours=12)
    coordinator, _ = _make_coordinator(tmp_path)
    _seed_meter(coordinator, START, now)
    coordinator._last_finalized_slot_utc = now - timedelta(minutes=15)

    assert coordinator._incremental_rebuild_start_utc() is None

    coordinator._rebuild_history_sync(now)

    # Last finalized slot is 11:45; the window reaches two hours back from it.
    assert coordinator._incremental_rebuild_start_utc() == datetime(
        2026, 9, 5, 9, 45, tzinfo=UTC
    )


def _seed_events(
    coordinator: HouseholdForecastCoordinator,
    key: str,
    states: list[tuple[datetime, float]],
) -> None:
    db = coordinator._ensure_db()
    db.executemany(
        "INSERT OR REPLACE INTO raw_events "
        "(ts_utc, event_key, state_num, state_text, quality) VALUES (?, ?, ?, ?, ?)",
        [
            (_ts(moment), key, state, "on" if state else "off", "ok")
            for moment, state in states
        ],
    )
    db.commit()


def _appliance_coordinator(tmp_path: Path) -> HouseholdForecastCoordinator:
    coordinator, _ = _make_coordinator(
        tmp_path,
        extra_options={
            CONF_WATER_HEATER_POWER_ENTITY: "sensor.water_heater_power",
            CONF_WATER_HEATER_POWER_W: 3000.0,
        },
    )
    return coordinator


def test_quiet_appliance_power_sensor_does_not_fall_back_to_active_events(
    tmp_path: Path,
) -> None:
    coordinator = _appliance_coordinator(tmp_path)
    window = START + timedelta(days=1)
    db = coordinator._ensure_db()
    db.executemany(
        "INSERT INTO raw_samples (ts_utc, sensor_key, value, unit, quality) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                _ts(START + timedelta(hours=1)),
                CONF_WATER_HEATER_POWER_ENTITY,
                0.0,
                "W",
                "ok",
            ),
            (
                _ts(START + timedelta(hours=2)),
                CONF_WATER_HEATER_POWER_ENTITY,
                0.0,
                "W",
                "ok",
            ),
        ],
    )
    db.commit()
    _seed_events(
        coordinator,
        CONF_WATER_HEATER_ACTIVE_ENTITY,
        [(window + timedelta(hours=1), 1.0), (window + timedelta(hours=2), 0.0)],
    )

    rows = coordinator._load_appliance_interval_rows_sync(
        CONF_WATER_HEATER_POWER_ENTITY,
        active_key=CONF_WATER_HEATER_ACTIVE_ENTITY,
        fallback_kw=3.0,
        since_utc=window,
    )

    assert rows == []


def test_appliance_without_power_history_falls_back_to_active_events(
    tmp_path: Path,
) -> None:
    coordinator = _appliance_coordinator(tmp_path)
    window = START + timedelta(days=1)
    _seed_events(
        coordinator,
        CONF_WATER_HEATER_ACTIVE_ENTITY,
        [
            (window - timedelta(hours=1), 0.0),
            (window + timedelta(hours=1), 1.0),
            (window + timedelta(hours=2), 0.0),
        ],
    )

    rows = coordinator._load_appliance_interval_rows_sync(
        CONF_WATER_HEATER_POWER_ENTITY,
        active_key=CONF_WATER_HEATER_ACTIVE_ENTITY,
        fallback_kw=3.0,
        since_utc=window,
    )

    assert [row.delta_kwh for row in rows] == [pytest.approx(3.0)]
    assert rows[0].ts_from_utc == window + timedelta(hours=1)


def test_event_window_keeps_the_state_active_at_the_window_start(
    tmp_path: Path,
) -> None:
    coordinator = _appliance_coordinator(tmp_path)
    window = START + timedelta(days=1)
    _seed_events(
        coordinator,
        CONF_WATER_HEATER_ACTIVE_ENTITY,
        [(window - timedelta(hours=1), 1.0), (window + timedelta(hours=1), 0.0)],
    )

    samples = coordinator._load_event_samples_sync(
        CONF_WATER_HEATER_ACTIVE_ENTITY, window
    )
    rows = coordinator._build_active_interval_energy_rows(samples, 3.0)

    assert [row.interval_sec for row in rows] == [7200]


def test_numeric_window_includes_only_one_anchor_sample(tmp_path: Path) -> None:
    coordinator, _ = _make_coordinator(tmp_path)
    _seed_meter(coordinator, START, START + timedelta(hours=1))
    since = START + timedelta(minutes=30)

    samples = coordinator._load_numeric_samples_sync(METER, since)

    assert samples[0].ts_utc == since - STEP + OFFSET
    assert [s.ts_utc for s in samples[1:3]] == [since + OFFSET, since + STEP + OFFSET]
    assert samples[-1].ts_utc == START + timedelta(hours=1) - STEP + OFFSET
    assert len(coordinator._load_numeric_samples_sync(METER)) == 12
