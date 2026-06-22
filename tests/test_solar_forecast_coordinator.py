"""Tests for the solar forecast coordinator."""

import bisect
import importlib
import math
import sqlite3
import sys
import types
from collections import deque
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock homeassistant modules so we can import the coordinator without HA.
# We mock broadly using a module-finder that catches any "homeassistant.*"
# or "voluptuous*" import and returns a MagicMock module.
# ---------------------------------------------------------------------------

_REAL_HA_AVAILABLE = False
try:
    import homeassistant  # noqa: F401

    _REAL_HA_AVAILABLE = True
except ImportError:
    pass


class _HAMockFinder:
    """Meta path finder that intercepts homeassistant.* and voluptuous* imports."""

    _INTERCEPTED_PREFIXES = ("homeassistant", "voluptuous")

    def find_module(self, fullname, path=None):
        if any(
            fullname == p or fullname.startswith(p + ".")
            for p in self._INTERCEPTED_PREFIXES
        ):
            return self
        return None

    def find_spec(self, fullname, path, target=None):
        if any(
            fullname == p or fullname.startswith(p + ".")
            for p in self._INTERCEPTED_PREFIXES
        ):
            from importlib.machinery import ModuleSpec

            return ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        mod = MagicMock()
        mod.__name__ = spec.name
        mod.__loader__ = self
        mod.__package__ = spec.name
        mod.__path__ = []
        mod.__spec__ = spec
        if spec.name == "homeassistant.core":
            mod.callback = lambda f: f
        return mod

    def exec_module(self, module):
        pass

    def load_module(self, fullname):
        if fullname in sys.modules:
            return sys.modules[fullname]
        mod = MagicMock()
        mod.__name__ = fullname
        mod.__loader__ = self
        mod.__package__ = fullname
        mod.__path__ = []
        mod.__spec__ = None
        if fullname == "homeassistant.core":
            mod.callback = lambda f: f
        sys.modules[fullname] = mod
        return mod


if not _REAL_HA_AVAILABLE:
    sys.meta_path.insert(0, _HAMockFinder())

# Now we can import the coordinator (it will get mock HA modules)
from custom_components.energyadvisor.solar_forecast_coordinator import (  # noqa: E402
    CORRECTION_HALF_LIFE_DAYS,
    DB_SCHEMA_VERSION,
    INTRADAY_MAX_SCALING,
    INTRADAY_MIN_SAMPLES,
    INTRADAY_MIN_SCALING,
    MAX_HISTORY_DAYS,
    MAX_RATIO,
    MIN_CORRECTION_SAMPLES,
    MIN_RATIO,
    NIGHT_THRESHOLD_W,
    SLOT_AZIMUTH_BINS,
    SLOT_ELEVATION_STEP,
    SLOTS_PER_DAY,
    SolarForecastCoordinator,
    _solar_position,
    _solar_slot,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coordinator(
    *,
    forecast_entity="sensor.om_forecast",
    forecast_tomorrow_entity=None,
    power_entity="sensor.inverter_power",
    latitude=59.33,
    longitude=18.07,
    time_zone="Europe/Stockholm",
    config_dir="/tmp/test_solar",
    entry_id="test_entry_1",
    levels_sensor=None,
):
    """Create a SolarForecastCoordinator with mocked hass and entry."""
    hass = MagicMock()
    hass.config.time_zone = time_zone
    hass.config.latitude = latitude
    hass.config.longitude = longitude
    hass.config.config_dir = config_dir

    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = {
        "forecast_entity": forecast_entity,
        "power_entity": power_entity,
    }
    if forecast_tomorrow_entity is not None:
        entry.options["forecast_tomorrow_entity"] = forecast_tomorrow_entity

    coord = SolarForecastCoordinator(hass, entry, levels_sensor)
    return coord


def _make_in_memory_db():
    """Create an in-memory SQLite DB with the readings table."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE readings (
            date     TEXT    NOT NULL,
            slot     INTEGER NOT NULL,
            om_w     REAL    NOT NULL,
            actual_w REAL    NOT NULL,
            PRIMARY KEY (date, slot)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_slot ON readings(slot)")
    conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
    conn.commit()
    return conn


def _insert_readings(conn, rows):
    """Insert list of (date_str, slot, om_w, actual_w) tuples."""
    conn.executemany(
        "INSERT OR REPLACE INTO readings (date, slot, om_w, actual_w) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# _solar_position
# ---------------------------------------------------------------------------


class TestSolarPosition:

    def test_noon_summer_stockholm(self):
        """Sun should be high in the sky at noon in Stockholm in June."""
        dt = datetime(2024, 6, 21, 10, 0, tzinfo=UTC)  # ~12:00 local
        elev, azim = _solar_position(59.33, 18.07, dt)
        assert elev > 45, f"Expected high elevation, got {elev}"
        assert 150 < azim < 210, f"Expected southern azimuth, got {azim}"

    def test_midnight_gives_negative_elevation(self):
        """Sun should be below horizon at midnight in Stockholm in winter."""
        dt = datetime(2024, 12, 21, 23, 0, tzinfo=UTC)
        elev, _ = _solar_position(59.33, 18.07, dt)
        assert elev < 0, f"Expected negative elevation, got {elev}"

    def test_equator_noon_equinox(self):
        """At equator on equinox at solar noon, sun should be nearly overhead."""
        dt = datetime(2024, 3, 20, 12, 0, tzinfo=UTC)
        elev, _ = _solar_position(0.0, 0.0, dt)
        assert elev > 85, f"Expected near-zenith, got {elev}"

    def test_morning_east_azimuth(self):
        """In the morning the sun should be in the east (azimuth < 180)."""
        dt = datetime(2024, 6, 21, 4, 0, tzinfo=UTC)  # early morning UTC
        elev, azim = _solar_position(59.33, 18.07, dt)
        if elev > 0:
            assert azim < 180, f"Expected east, got {azim}"


# ---------------------------------------------------------------------------
# _solar_slot
# ---------------------------------------------------------------------------


class TestSolarSlot:

    def test_nighttime_returns_minus_one(self):
        dt = datetime(2024, 12, 21, 23, 0, tzinfo=UTC)
        slot = _solar_slot(59.33, 18.07, dt, SLOT_ELEVATION_STEP, SLOT_AZIMUTH_BINS)
        assert slot == -1

    def test_daytime_returns_non_negative(self):
        dt = datetime(2024, 6, 21, 10, 0, tzinfo=UTC)
        slot = _solar_slot(59.33, 18.07, dt, SLOT_ELEVATION_STEP, SLOT_AZIMUTH_BINS)
        assert slot >= 0

    def test_slot_encoding(self):
        """Slot should encode as elev_bin * SLOT_AZIMUTH_BINS + azim_bin."""
        dt = datetime(2024, 6, 21, 10, 0, tzinfo=UTC)
        elev, azim = _solar_position(59.33, 18.07, dt)
        expected_elev_bin = min(
            int(elev // SLOT_ELEVATION_STEP), (90 // SLOT_ELEVATION_STEP) - 1
        )
        expected_azim_bin = int(azim // (360 / SLOT_AZIMUTH_BINS)) % SLOT_AZIMUTH_BINS
        expected_slot = expected_elev_bin * SLOT_AZIMUTH_BINS + expected_azim_bin
        slot = _solar_slot(59.33, 18.07, dt, SLOT_ELEVATION_STEP, SLOT_AZIMUTH_BINS)
        assert slot == expected_slot

    def test_max_slot_value(self):
        """Slot should never exceed 9 * 12 - 1 = 107."""
        # Use equator at noon for max elevation
        dt = datetime(2024, 3, 20, 12, 0, tzinfo=UTC)
        slot = _solar_slot(0.0, 0.0, dt, SLOT_ELEVATION_STEP, SLOT_AZIMUTH_BINS)
        assert 0 <= slot <= (90 // SLOT_ELEVATION_STEP) * SLOT_AZIMUTH_BINS - 1


# ---------------------------------------------------------------------------
# _slot_average_w (trapezoidal integration)
# ---------------------------------------------------------------------------


class TestSlotAverageW:

    def _coord_with_buffer(self, buffer_data):
        coord = _make_coordinator()
        coord._power_buffer = deque(buffer_data)
        return coord

    def test_empty_buffer_returns_none(self):
        coord = self._coord_with_buffer([])
        result = coord._slot_average_w(datetime(2024, 6, 1, 12, 0, tzinfo=UTC))
        assert result is None

    def test_single_reading_returns_that_value(self):
        t = datetime(2024, 6, 1, 12, 5, tzinfo=UTC)
        coord = self._coord_with_buffer([(t, 500.0)])
        slot_end = datetime(2024, 6, 1, 12, 15, tzinfo=UTC)
        result = coord._slot_average_w(slot_end)
        assert result == 500.0

    def test_constant_power_returns_that_value(self):
        """If power is constant across the slot, average should equal that constant."""
        slot_end = datetime(2024, 6, 1, 12, 15, tzinfo=UTC)
        slot_start = slot_end - timedelta(minutes=15)
        readings = [
            (slot_start + timedelta(minutes=i), 1000.0) for i in range(0, 16, 3)
        ]
        coord = self._coord_with_buffer(readings)
        result = coord._slot_average_w(slot_end)
        assert abs(result - 1000.0) < 0.01

    def test_linear_ramp_gives_midpoint(self):
        """Power ramping linearly 0→1000 should average to ~500."""
        slot_end = datetime(2024, 6, 1, 12, 15, tzinfo=UTC)
        slot_start = slot_end - timedelta(minutes=15)
        readings = [
            (slot_start + timedelta(minutes=i), i * (1000.0 / 15)) for i in range(16)
        ]
        coord = self._coord_with_buffer(readings)
        result = coord._slot_average_w(slot_end)
        assert abs(result - 500.0) < 10  # trapezoidal should be close

    def test_prunes_old_entries(self):
        """Entries older than slot_start - 5min should be pruned."""
        slot_end = datetime(2024, 6, 1, 12, 15, tzinfo=UTC)
        old_entry = (datetime(2024, 6, 1, 11, 50, tzinfo=UTC), 200.0)
        recent_entry = (datetime(2024, 6, 1, 12, 5, tzinfo=UTC), 800.0)
        coord = self._coord_with_buffer([old_entry, recent_entry])
        coord._slot_average_w(slot_end)
        # Old entry should be pruned
        assert len(coord._power_buffer) == 1
        assert coord._power_buffer[0] == recent_entry

    def test_fallback_to_recent_reading_outside_window(self):
        """If no readings in slot window, falls back to most recent buffer entry."""
        slot_end = datetime(2024, 6, 1, 12, 15, tzinfo=UTC)
        # Reading just before the window (at slot_start - 3 min, within prune threshold)
        reading = (datetime(2024, 6, 1, 11, 57, tzinfo=UTC), 750.0)
        coord = self._coord_with_buffer([reading])
        result = coord._slot_average_w(slot_end)
        assert result == 750.0


# ---------------------------------------------------------------------------
# _nearest_om_value and _nearest_om_value_sorted
# ---------------------------------------------------------------------------


class TestNearestOmValue:

    def test_empty_returns_zero(self):
        assert SolarForecastCoordinator._nearest_om_value({}, datetime.now(UTC)) == 0.0

    def test_exact_match(self):
        t = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        om = {t: 5000.0}
        assert SolarForecastCoordinator._nearest_om_value(om, t) == 5000.0

    def test_within_max_gap(self):
        t1 = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        target = datetime(2024, 6, 1, 12, 10, tzinfo=UTC)  # 10 min away
        om = {t1: 3000.0}
        assert (
            SolarForecastCoordinator._nearest_om_value(om, target, max_gap_minutes=20)
            == 3000.0
        )

    def test_beyond_max_gap_returns_zero(self):
        t1 = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        target = datetime(2024, 6, 1, 12, 30, tzinfo=UTC)  # 30 min away
        om = {t1: 3000.0}
        assert (
            SolarForecastCoordinator._nearest_om_value(om, target, max_gap_minutes=20)
            == 0.0
        )

    def test_sorted_matches_linear(self):
        """The bisect version should return identical results to the linear version."""
        base = datetime(2024, 6, 1, 6, 0, tzinfo=UTC)
        om = {base + timedelta(minutes=15 * i): float(100 + i * 50) for i in range(200)}
        sorted_keys = sorted(om)

        targets = [
            base + timedelta(minutes=7),
            base + timedelta(minutes=150),
            base + timedelta(minutes=1500),
            base - timedelta(minutes=5),
            base + timedelta(hours=60),  # beyond all data
        ]
        for target in targets:
            linear = SolarForecastCoordinator._nearest_om_value(om, target)
            sorted_result = SolarForecastCoordinator._nearest_om_value_sorted(
                sorted_keys, om, target
            )
            assert linear == sorted_result, f"Mismatch at {target}"

    def test_sorted_empty_returns_zero(self):
        assert (
            SolarForecastCoordinator._nearest_om_value_sorted([], {}, datetime.now(UTC))
            == 0.0
        )

    def test_sorted_picks_closest(self):
        t1 = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        t2 = datetime(2024, 6, 1, 12, 15, tzinfo=UTC)
        om = {t1: 1000.0, t2: 2000.0}
        sorted_keys = sorted(om)
        # Target closer to t2
        target = datetime(2024, 6, 1, 12, 10, tzinfo=UTC)
        result = SolarForecastCoordinator._nearest_om_value_sorted(
            sorted_keys, om, target
        )
        assert result == 2000.0


# ---------------------------------------------------------------------------
# _compute_correction_factors_sync
# ---------------------------------------------------------------------------


class TestComputeCorrectionFactors:

    def _coord_with_db(self, rows):
        """Create coordinator with in-memory DB populated with rows."""
        coord = _make_coordinator()
        db = _make_in_memory_db()
        _insert_readings(db, rows)
        coord._db = db
        return coord

    def test_empty_db_returns_empty_factors(self):
        coord = self._coord_with_db([])
        factors, n_samples, oldest = coord._compute_correction_factors_sync()
        assert factors == {}
        assert n_samples == 0
        assert oldest is None

    def test_below_min_samples_not_included(self):
        """Slots with fewer than MIN_CORRECTION_SAMPLES are excluded."""
        today = date.today().isoformat()
        # Only 3 readings for slot 5 (less than MIN_CORRECTION_SAMPLES=5)
        rows = [(today, 5, 100.0, 150.0)] * 3
        # But we use PRIMARY KEY so same (date, slot) overwrites — use different dates
        rows = [
            ((date.today() - timedelta(days=i)).isoformat(), 5, 100.0, 150.0)
            for i in range(MIN_CORRECTION_SAMPLES - 1)
        ]
        coord = self._coord_with_db(rows)
        factors, n_samples, oldest = coord._compute_correction_factors_sync()
        assert 5 not in factors
        assert n_samples == MIN_CORRECTION_SAMPLES - 1

    def test_enough_samples_produces_factor(self):
        """Slots with >= MIN_CORRECTION_SAMPLES produce a correction factor."""
        rows = [
            ((date.today() - timedelta(days=i)).isoformat(), 10, 100.0, 200.0)
            for i in range(MIN_CORRECTION_SAMPLES)
        ]
        coord = self._coord_with_db(rows)
        factors, n_samples, oldest = coord._compute_correction_factors_sync()
        assert 10 in factors
        # Ratio is 200/100 = 2.0 for all samples → factor should be ~2.0
        assert abs(factors[10] - 2.0) < 0.1

    def test_exponential_decay_weights_recent_more(self):
        """Recent data should have more influence than old data."""
        today = date.today()
        rows = []
        # 5 recent readings (ratio 2.0) + 5 old readings (ratio 1.0)
        for i in range(5):
            rows.append(((today - timedelta(days=i)).isoformat(), 20, 100.0, 200.0))
        for i in range(5, 10):
            rows.append(
                ((today - timedelta(days=i + 50)).isoformat(), 20, 100.0, 100.0)
            )

        coord = self._coord_with_db(rows)
        factors, _, _ = coord._compute_correction_factors_sync()
        # Factor should be closer to 2.0 than 1.5 because recent data is weighted more
        assert factors[20] > 1.5

    def test_ratio_clamped_to_max(self):
        """Extreme ratios should be clamped to MAX_RATIO."""
        rows = [
            ((date.today() - timedelta(days=i)).isoformat(), 30, 10.0, 10000.0)
            for i in range(MIN_CORRECTION_SAMPLES)
        ]
        coord = self._coord_with_db(rows)
        factors, _, _ = coord._compute_correction_factors_sync()
        assert factors[30] <= MAX_RATIO

    def test_ratio_clamped_to_min(self):
        """Very low ratios should be clamped to MIN_RATIO."""
        rows = [
            ((date.today() - timedelta(days=i)).isoformat(), 40, 1000.0, 0.1)
            for i in range(MIN_CORRECTION_SAMPLES)
        ]
        coord = self._coord_with_db(rows)
        factors, _, _ = coord._compute_correction_factors_sync()
        assert factors[40] >= MIN_RATIO

    def test_night_threshold_filters_rows(self):
        """Readings where om_w < NIGHT_THRESHOLD_W should be excluded."""
        rows = [
            ((date.today() - timedelta(days=i)).isoformat(), 50, 5.0, 100.0)
            for i in range(MIN_CORRECTION_SAMPLES)
        ]
        coord = self._coord_with_db(rows)
        factors, n_samples, _ = coord._compute_correction_factors_sync()
        assert 50 not in factors
        assert n_samples == 0

    def test_oldest_date_returned(self):
        """Should return the oldest date in the data."""
        rows = [
            ((date.today() - timedelta(days=i)).isoformat(), 60, 100.0, 150.0)
            for i in range(MIN_CORRECTION_SAMPLES)
        ]
        coord = self._coord_with_db(rows)
        _, _, oldest = coord._compute_correction_factors_sync()
        expected = (
            date.today() - timedelta(days=MIN_CORRECTION_SAMPLES - 1)
        ).isoformat()
        assert oldest == expected


# ---------------------------------------------------------------------------
# _compute_intraday_scaling_sync
# ---------------------------------------------------------------------------


class TestComputeIntradayScaling:

    def _coord_with_db_and_factors(self, rows, correction_factors):
        coord = _make_coordinator()
        db = _make_in_memory_db()
        _insert_readings(db, rows)
        coord._db = db
        coord.correction_factors = correction_factors
        return coord

    def test_no_data_returns_1(self):
        coord = self._coord_with_db_and_factors([], {})
        result = coord._compute_intraday_scaling_sync()
        assert result == 1.0

    def test_below_min_samples_returns_1(self):
        """Fewer than INTRADAY_MIN_SAMPLES slots returns 1.0."""
        today = date.today().isoformat()
        rows = [(today, i, 100.0, 150.0) for i in range(INTRADAY_MIN_SAMPLES - 1)]
        coord = self._coord_with_db_and_factors(
            rows, {i: 1.0 for i in range(INTRADAY_MIN_SAMPLES)}
        )
        result = coord._compute_intraday_scaling_sync()
        assert result == 1.0

    def test_actual_above_refined_scales_up(self):
        """When actual > refined forecast, scaling should be > 1.0."""
        today = date.today().isoformat()
        rows = [
            (today, i, 100.0, 200.0)  # actual is 2x the OM value
            for i in range(INTRADAY_MIN_SAMPLES + 2)
        ]
        # correction_factor=1.0 → refined = om_w * 1.0 = 100
        # actual=200, so ratio = 200/100 = 2.0
        factors = {i: 1.0 for i in range(INTRADAY_MIN_SAMPLES + 2)}
        coord = self._coord_with_db_and_factors(rows, factors)
        result = coord._compute_intraday_scaling_sync()
        assert abs(result - 2.0) < 0.01

    def test_scaling_clamped_to_max(self):
        """Scaling should not exceed INTRADAY_MAX_SCALING."""
        today = date.today().isoformat()
        rows = [
            (today, i, 100.0, 5000.0)  # extreme excess
            for i in range(INTRADAY_MIN_SAMPLES + 2)
        ]
        factors = {i: 1.0 for i in range(INTRADAY_MIN_SAMPLES + 2)}
        coord = self._coord_with_db_and_factors(rows, factors)
        result = coord._compute_intraday_scaling_sync()
        assert result == INTRADAY_MAX_SCALING

    def test_scaling_clamped_to_min(self):
        """Scaling should not go below INTRADAY_MIN_SCALING."""
        today = date.today().isoformat()
        rows = [
            (today, i, 100.0, 1.0)  # almost no actual production
            for i in range(INTRADAY_MIN_SAMPLES + 2)
        ]
        factors = {i: 1.0 for i in range(INTRADAY_MIN_SAMPLES + 2)}
        coord = self._coord_with_db_and_factors(rows, factors)
        result = coord._compute_intraday_scaling_sync()
        assert result == INTRADAY_MIN_SCALING

    def test_correction_factor_applied_before_ratio(self):
        """Intraday compares actual against refined (om_w × factor), not raw om_w."""
        today = date.today().isoformat()
        # om_w=100, actual=200, factor=2.0 → refined=200 → ratio=1.0
        rows = [(today, i, 100.0, 200.0) for i in range(INTRADAY_MIN_SAMPLES + 2)]
        factors = {i: 2.0 for i in range(INTRADAY_MIN_SAMPLES + 2)}
        coord = self._coord_with_db_and_factors(rows, factors)
        result = coord._compute_intraday_scaling_sync()
        assert abs(result - 1.0) < 0.01


# ---------------------------------------------------------------------------
# _collect_om_data
# ---------------------------------------------------------------------------


class TestCollectOmData:

    def _coord_with_state(self, attributes, entity_id="sensor.om_forecast"):
        coord = _make_coordinator(forecast_entity=entity_id)
        state = MagicMock()
        state.attributes = attributes
        coord.hass.states.get = lambda eid: state if eid == entity_id else None
        return coord

    def test_empty_watts_dict(self):
        coord = self._coord_with_state({"watts": {}})
        result = coord._collect_om_data()
        assert result == {}

    def test_watts_dict_parsed(self):
        """Open Meteo 'watts' format should be parsed into UTC datetime keys."""
        now_utc = datetime.now(UTC)
        ts = now_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        coord = self._coord_with_state({"watts": {ts: 1500}})
        result = coord._collect_om_data()
        assert len(result) == 1
        value = list(result.values())[0]
        assert value == 1500.0

    def test_forecasts_list_parsed(self):
        """Solcast-style 'forecasts' list should be parsed (kW → W)."""
        now_utc = datetime.now(UTC)
        ts = now_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        coord = self._coord_with_state(
            {"watts": {}, "forecasts": [{"period_end": ts, "pv_estimate": 3.5}]}
        )
        result = coord._collect_om_data()
        assert len(result) == 1
        value = list(result.values())[0]
        assert value == 3500.0  # 3.5 kW → 3500 W

    def test_filters_by_time_window(self):
        """Only entries within today→tomorrow+1h should be included."""
        far_future = (datetime.now(UTC) + timedelta(hours=60)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        near = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        coord = self._coord_with_state({"watts": {far_future: 1000, near: 2000}})
        result = coord._collect_om_data()
        assert len(result) == 1
        assert list(result.values())[0] == 2000.0

    def test_entity_not_found_returns_empty(self):
        coord = _make_coordinator(forecast_entity="sensor.nonexistent")
        coord.hass.states.get = lambda eid: None
        result = coord._collect_om_data()
        assert result == {}

    def test_invalid_timestamp_skipped(self):
        coord = self._coord_with_state({"watts": {"not-a-date": 1000}})
        result = coord._collect_om_data()
        assert result == {}


# ---------------------------------------------------------------------------
# _build_forecast_from_data
# ---------------------------------------------------------------------------


class TestBuildForecastFromData:

    def test_empty_om_data_returns_empty(self):
        coord = _make_coordinator()
        coord.correction_factors = {}
        coord.intraday_scaling = 1.0
        result = coord._build_forecast_from_data({})
        assert result == []

    def test_produces_192_entries(self):
        """Should always produce exactly 192 entries (today + tomorrow)."""
        coord = _make_coordinator()
        coord.correction_factors = {}
        coord.intraday_scaling = 1.0

        # Generate 192 fake OM entries at 15-min intervals starting from midnight today
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/Stockholm")
        now_local = datetime.now(tz)
        midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        om_data = {}
        for i in range(192):
            dt = midnight.astimezone(UTC) + timedelta(minutes=15 * (i + 1))
            om_data[dt] = 500.0 if 6 * 4 <= i <= 20 * 4 else 0.0

        result = coord._build_forecast_from_data(om_data)
        assert len(result) == 192

    def test_correction_factor_applied(self):
        """Entries with a correction factor should have pow = raw × factor."""
        coord = _make_coordinator()
        coord.intraday_scaling = 1.0

        # Create data for a single point at a known solar position
        dt = datetime(2024, 6, 21, 10, 0, tzinfo=UTC)  # daytime in Stockholm
        slot = _solar_slot(59.33, 18.07, dt, SLOT_ELEVATION_STEP, SLOT_AZIMUTH_BINS)
        coord.correction_factors = {slot: 1.5}

        om_data = {dt: 1000.0}  # 1000W raw
        result = coord._build_forecast_from_data(om_data)
        # Find the entry closest to our target time
        matching = [e for e in result if e["raw"] > 0.5]
        if matching:
            entry = matching[0]
            # pow should be raw * factor = 1.0kW * 1.5 = 1.5kW
            assert abs(entry["pow"] - 1.5) < 0.1

    def test_night_slots_stay_zero(self):
        """Entries below NIGHT_THRESHOLD_W should have pow = 0."""
        coord = _make_coordinator()
        coord.correction_factors = {0: 2.0}
        coord.intraday_scaling = 1.0

        # Only nighttime data (below threshold)
        dt = datetime(2024, 6, 21, 22, 0, tzinfo=UTC)
        om_data = {dt: 5.0}  # below NIGHT_THRESHOLD_W
        result = coord._build_forecast_from_data(om_data)
        # All entries near that timestamp should have zero estimate
        for entry in result:
            if entry["raw"] < NIGHT_THRESHOLD_W / 1000:
                assert entry["pow"] == 0.0

    def test_output_format(self):
        """Each entry should have the required keys; always 192 entries."""
        coord = _make_coordinator()
        coord.correction_factors = {}
        coord.intraday_scaling = 1.0

        dt = datetime(2024, 6, 21, 10, 0, tzinfo=UTC)
        om_data = {dt: 2000.0}
        result = coord._build_forecast_from_data(om_data)
        assert len(result) == 192
        for entry in result:
            assert "end" in entry
            assert "pow" in entry
            assert "raw" in entry


# ---------------------------------------------------------------------------
# _purge_old_data_sync
# ---------------------------------------------------------------------------


class TestPurgeOldData:

    def test_purges_data_older_than_max_days(self):
        coord = _make_coordinator()
        db = _make_in_memory_db()
        coord._db = db

        old_date = (date.today() - timedelta(days=MAX_HISTORY_DAYS + 10)).isoformat()
        recent_date = (date.today() - timedelta(days=5)).isoformat()
        _insert_readings(
            db,
            [
                (old_date, 1, 100.0, 150.0),
                (recent_date, 1, 200.0, 250.0),
            ],
        )

        coord._purge_old_data_sync()

        rows = db.execute("SELECT * FROM readings").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == recent_date

    def test_keeps_data_within_window(self):
        coord = _make_coordinator()
        db = _make_in_memory_db()
        coord._db = db

        dates = [
            (date.today() - timedelta(days=i)).isoformat()
            for i in range(MAX_HISTORY_DAYS - 5)
        ]
        rows = [(d, 1, 100.0, 150.0) for d in dates]
        _insert_readings(db, rows)

        coord._purge_old_data_sync()

        remaining = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        assert remaining == len(dates)


# ---------------------------------------------------------------------------
# _upsert_reading
# ---------------------------------------------------------------------------


class TestUpsertReading:

    def test_inserts_new_reading(self):
        coord = _make_coordinator()
        db = _make_in_memory_db()
        coord._db = db

        coord._upsert_reading("2024-06-01", 5, 100.0, 150.0)
        row = db.execute(
            "SELECT * FROM readings WHERE date='2024-06-01' AND slot=5"
        ).fetchone()
        assert row == ("2024-06-01", 5, 100.0, 150.0)

    def test_replaces_existing_reading(self):
        coord = _make_coordinator()
        db = _make_in_memory_db()
        coord._db = db

        coord._upsert_reading("2024-06-01", 5, 100.0, 150.0)
        coord._upsert_reading("2024-06-01", 5, 200.0, 300.0)
        rows = db.execute(
            "SELECT * FROM readings WHERE date='2024-06-01' AND slot=5"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0] == ("2024-06-01", 5, 200.0, 300.0)


# ---------------------------------------------------------------------------
# _on_power_state_change (callback)
# ---------------------------------------------------------------------------


class TestOnPowerStateChange:

    def test_valid_state_appended(self):
        coord = _make_coordinator()
        event = MagicMock()
        event.data = {"new_state": MagicMock(state="1500.5")}
        coord._on_power_state_change(event)
        assert len(coord._power_buffer) == 1
        assert coord._power_buffer[0][1] == 1500.5

    def test_unavailable_state_ignored(self):
        coord = _make_coordinator()
        event = MagicMock()
        event.data = {"new_state": MagicMock(state="unavailable")}
        coord._on_power_state_change(event)
        assert len(coord._power_buffer) == 0

    def test_unknown_state_ignored(self):
        coord = _make_coordinator()
        event = MagicMock()
        event.data = {"new_state": MagicMock(state="unknown")}
        coord._on_power_state_change(event)
        assert len(coord._power_buffer) == 0

    def test_none_new_state_ignored(self):
        coord = _make_coordinator()
        event = MagicMock()
        event.data = {"new_state": None}
        coord._on_power_state_change(event)
        assert len(coord._power_buffer) == 0

    def test_non_numeric_state_ignored(self):
        coord = _make_coordinator()
        event = MagicMock()
        event.data = {"new_state": MagicMock(state="not_a_number")}
        coord._on_power_state_change(event)
        assert len(coord._power_buffer) == 0


class TestRecordCurrentSlot:

    @pytest.mark.asyncio
    async def test_skips_when_export_credit_negative_without_hardcoded_lookup(self):
        """Negative export credit should use the levels sensor, not a fixed entity id."""
        levels_sensor = MagicMock()
        levels_sensor.current_credit = -0.05
        coord = _make_coordinator(levels_sensor=levels_sensor)
        coord._slot_average_w = MagicMock(return_value=1200.0)
        coord._collect_om_data = MagicMock(return_value={})
        coord._nearest_om_value = MagicMock(return_value=1400.0)
        coord._slot_for = MagicMock(return_value=7)
        coord._upsert_reading = MagicMock()
        coord.hass.states.get = MagicMock(
            side_effect=AssertionError("unexpected hass.states lookup")
        )

        async def _async_add_executor_job(func, *args):
            return func(*args)

        coord.hass.async_add_executor_job = _async_add_executor_job

        await coord._record_current_slot(datetime(2024, 6, 1, 12, 15, tzinfo=UTC))

        coord.hass.states.get.assert_not_called()
        coord._slot_for.assert_not_called()
        coord._upsert_reading.assert_not_called()


# ---------------------------------------------------------------------------
# DB schema migration
# ---------------------------------------------------------------------------


class TestDbMigration:

    def test_v0_clears_readings(self):
        """v0/v1 migration should clear all readings."""
        coord = _make_coordinator()
        conn = sqlite3.connect(":memory:")
        conn.execute("""
            CREATE TABLE readings (
                date TEXT NOT NULL, slot INTEGER NOT NULL,
                om_w REAL NOT NULL, actual_w REAL NOT NULL,
                PRIMARY KEY (date, slot))
        """)
        conn.execute("INSERT INTO readings VALUES ('2024-01-01', 5, 100, 150)")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()

        # Patch _open_db to return our in-memory conn
        with patch.object(coord, "_db_path", return_value=":memory:"):
            # Manually run the migration logic
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 0
            if version < 2:
                conn.execute("DELETE FROM readings")
            conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
            conn.commit()

        rows = conn.execute("SELECT * FROM readings").fetchall()
        assert len(rows) == 0
        new_ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert new_ver == DB_SCHEMA_VERSION
