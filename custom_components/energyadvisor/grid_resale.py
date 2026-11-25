"""Durable accounting for grid energy charged to a battery and later resold."""

from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .const import (
    CONF_BATTERY_CHARGE_POWER_ENTITY,
    CONF_GRID_EXPORT_ENTITY,
    CONF_GRID_IMPORT_ENTITY,
    CONF_POWER_ENTITY,
)

STOCKHOLM = ZoneInfo("Europe/Stockholm")
RULE_VERSION = 1
CONF_RESALE_SOURCES_NEED_REVIEW = "grid_resale_sources_need_review"
POWER_KEYS = {
    "grid_import": CONF_GRID_IMPORT_ENTITY,
    "grid_export": CONF_GRID_EXPORT_ENTITY,
    "battery": CONF_BATTERY_CHARGE_POWER_ENTITY,
    "solar": CONF_POWER_ENTITY,
}
_LEGACY_POWER_KEYS = {
    "grid_import": "resale_grid_import_entity",
    "grid_export": "resale_grid_export_entity",
    "battery": "resale_battery_power_entity",
    "solar": "resale_solar_power_entity",
}
MAX_CAPTURE_GAP = 120


def migrate_resale_options(options: dict) -> tuple[dict, bool]:
    """Reuse shared inputs without silently replacing another feature's sources."""
    migrated = dict(options)
    migrated.pop("grid_resale_enabled", None)
    conflict = False
    for role, legacy_key in _LEGACY_POWER_KEYS.items():
        entity = migrated.pop(legacy_key, None)
        if not entity:
            continue
        shared_key = POWER_KEYS[role]
        if not migrated.get(shared_key):
            migrated[shared_key] = entity
        elif migrated[shared_key] != entity:
            conflict = True
    if migrated.pop("grid_resale_positive_charge", True) is False:
        conflict = True
    if conflict:
        migrated[CONF_RESALE_SOURCES_NEED_REVIEW] = True
    return migrated, bool(migrated.get(CONF_RESALE_SOURCES_NEED_REVIEW))


def resale_sources_configured(options: dict) -> bool:
    """Require distinct shared inputs and acknowledgement of legacy conflicts."""
    selected = [options.get(key) for key in POWER_KEYS.values()]
    return (
        not options.get(CONF_RESALE_SOURCES_NEED_REVIEW)
        and all(isinstance(entity, str) and entity for entity in selected)
        and len(set(selected)) == len(POWER_KEYS)
    )


def power_kw(value: object, unit: str | None) -> float | None:
    """Reject unavailable readings and unsupported units instead of assuming W."""
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(number) or unit not in ("W", "kW"):
        return None
    return number / 1000 if unit == "W" else number


def flows(values: dict[str, float], hours: float) -> tuple[float, float]:
    """Return conservative grid-to-battery and battery-to-grid energy in kWh.

    All PV is allocated to charging/export first, so ambiguous solar flows
    cannot create an allowance or an eligible export. Measurement boundaries
    must be compatible; PV must not include battery inverter output.
    """
    solar = values["solar"]
    charge = min(values["grid_import"], max(0.0, values["battery"] - solar))
    exported = min(
        max(0.0, -values["battery"]), max(0.0, values["grid_export"] - solar)
    )
    return charge * hours, exported * hours


def year_segments(start: datetime, end: datetime):
    """Split an interval exactly at Swedish tax-year boundaries."""
    while start < end:
        year = start.astimezone(STOCKHOLM).year
        boundary = datetime(year + 1, 1, 1, tzinfo=STOCKHOLM).astimezone(timezone.utc)
        stop = min(end, boundary)
        yield year, start, stop
        start = stop


class ResaleLedger:
    """Keep source evidence and reproducible yearly allowances outside recorder."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def initialize(self) -> None:
        """Create a ledger, never deleting records on reload or schema changes."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError(f"Unsupported grid resale database version: {version}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS configurations (
                    id INTEGER PRIMARY KEY,
                    created_utc TEXT NOT NULL,
                    options_json TEXT NOT NULL UNIQUE,
                    rule_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY,
                    timestamp_utc TEXT NOT NULL,
                    source TEXT NOT NULL,
                    configuration_id INTEGER NOT NULL REFERENCES configurations(id),
                    evidence_json TEXT NOT NULL,
                    UNIQUE(timestamp_utc, source, configuration_id, evidence_json)
                );
                CREATE INDEX IF NOT EXISTS resale_sample_source ON samples(source);
                CREATE TABLE IF NOT EXISTS year_totals (
                    tax_year INTEGER PRIMARY KEY,
                    grid_charged_kwh REAL NOT NULL,
                    eligible_export_kwh REAL NOT NULL,
                    allowance_kwh REAL NOT NULL,
                    estimated_interval_count INTEGER NOT NULL,
                    interval_count INTEGER NOT NULL,
                    flagged_intervals INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intervals (
                    start_utc TEXT PRIMARY KEY,
                    end_utc TEXT NOT NULL,
                    tax_year INTEGER NOT NULL,
                    sample_id INTEGER NOT NULL REFERENCES samples(id),
                    quality TEXT NOT NULL,
                    grid_charge_kwh REAL NOT NULL,
                    battery_export_kwh REAL NOT NULL,
                    eligible_kwh REAL NOT NULL,
                    allowance_kwh REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS resale_year
                    ON intervals(tax_year, start_utc);
                CREATE INDEX IF NOT EXISTS resale_end ON intervals(end_utc);
                CREATE TABLE IF NOT EXISTS interval_revisions (
                    id INTEGER PRIMARY KEY,
                    superseded_utc TEXT NOT NULL,
                    replacement_sample_id INTEGER NOT NULL REFERENCES samples(id),
                    start_utc TEXT NOT NULL,
                    end_utc TEXT NOT NULL,
                    tax_year INTEGER NOT NULL,
                    sample_id INTEGER NOT NULL REFERENCES samples(id),
                    quality TEXT NOT NULL,
                    grid_charge_kwh REAL NOT NULL,
                    battery_export_kwh REAL NOT NULL,
                    eligible_kwh REAL NOT NULL,
                    allowance_kwh REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS imports (
                    id INTEGER PRIMARY KEY,
                    created_utc TEXT NOT NULL,
                    start_utc TEXT NOT NULL,
                    end_utc TEXT NOT NULL,
                    source TEXT NOT NULL,
                    inserted INTEGER NOT NULL,
                    skipped INTEGER NOT NULL
                );
                PRAGMA user_version=2;
                """)

    def configuration(self, options: dict) -> int:
        """Retain the source mapping and polarity used for each observation."""
        payload = json.dumps(options, sort_keys=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO configurations "
                "(created_utc, options_json, rule_version) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), payload, RULE_VERSION),
            )
            return conn.execute(
                "SELECT id FROM configurations WHERE options_json=?", (payload,)
            ).fetchone()[0]

    def note_gap(self, start: datetime, end: datetime, configuration_id: int) -> None:
        """Retain downtime evidence without preventing a later historical fill."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO samples "
                "(timestamp_utc, source, configuration_id, evidence_json) VALUES (?, ?, ?, ?)",
                (
                    start.isoformat(),
                    "restart_gap",
                    configuration_id,
                    json.dumps({"end_utc": end.isoformat(), "reason": "restart"}),
                ),
            )

    def record(
        self,
        start: datetime,
        end: datetime,
        values: dict[str, float] | None,
        evidence: dict,
        configuration_id: int,
        source: str = "live",
        quality: str = "measured",
    ) -> int:
        """Store evidence and nonoverlapping intervals atomically.

        Historical inserts replay affected years, including already-recorded
        exports. Export is processed before charging in the same interval.
        """
        with closing(self._connect()) as conn, conn:
            inserted, replay_years = self._record(
                conn, start, end, values, evidence, configuration_id, source, quality
            )
            for year in replay_years:
                self._replay(conn, year)
        return inserted

    def record_many(
        self, rows: list, configuration_id: int, source: str
    ) -> tuple[int, int]:
        """Commit a historical chunk and replay each affected year only once."""
        inserted = skipped = 0
        replay_years: set[int] = set()
        with closing(self._connect()) as conn, conn:
            for start, end, values, evidence, quality in rows:
                added, affected = self._record(
                    conn,
                    start,
                    end,
                    values,
                    evidence,
                    configuration_id,
                    source,
                    quality,
                )
                inserted += added
                skipped += not added
                replay_years.update(affected)
            for year in replay_years:
                self._replay(conn, year)
        return inserted, skipped

    def _record(
        self,
        conn: sqlite3.Connection,
        start: datetime,
        end: datetime,
        values: dict[str, float] | None,
        evidence: dict,
        configuration_id: int,
        source: str,
        quality: str,
    ) -> tuple[int, set[int]]:
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("A resale interval must have increasing aware timestamps")
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if values is not None and (
            set(values) != set(POWER_KEYS)
            or any(not math.isfinite(v) for v in values.values())
            or any(values[key] < 0 for key in ("grid_import", "grid_export", "solar"))
        ):
            raise ValueError("Invalid resale power readings")
        inserted = 0
        replay_years: set[int] = set()
        payload = json.dumps(evidence, sort_keys=True)
        conn.execute(
            "INSERT OR IGNORE INTO samples "
            "(timestamp_utc, source, configuration_id, evidence_json) VALUES (?, ?, ?, ?)",
            (start.isoformat(), source, configuration_id, payload),
        )
        sample_id = conn.execute(
            "SELECT id FROM samples WHERE timestamp_utc=? AND source=? "
            "AND configuration_id=? AND evidence_json=?",
            (start.isoformat(), source, configuration_id, payload),
        ).fetchone()[0]
        for year, begin, stop in year_segments(start, end):
            overlaps = conn.execute(
                "SELECT * FROM intervals WHERE start_utc < ? AND end_utc > ?",
                (stop.isoformat(), begin.isoformat()),
            ).fetchall()
            if overlaps:
                if (
                    source != "history_raw"
                    or quality != "raw_reconstruction"
                    or values is None
                    or any(
                        row["quality"] != "missing_or_stale"
                        or row["grid_charge_kwh"] != 0
                        or row["battery_export_kwh"] != 0
                        or row["eligible_kwh"] != 0
                        for row in overlaps
                    )
                ):
                    continue
                self._archive_invalid_intervals(conn, overlaps, begin, stop, sample_id)
                replay_years.add(year)
            charged, exported = (
                flows(values, (stop - begin).total_seconds() / 3600)
                if values is not None
                else (0.0, 0.0)
            )
            previous = conn.execute(
                "SELECT allowance_kwh FROM intervals WHERE tax_year=? "
                "AND end_utc<=? ORDER BY start_utc DESC LIMIT 1",
                (year, begin.isoformat()),
            ).fetchone()
            allowance = previous[0] if previous else 0.0
            eligible = min(allowance, exported)
            conn.execute(
                "INSERT INTO intervals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    begin.isoformat(),
                    stop.isoformat(),
                    year,
                    sample_id,
                    quality,
                    charged,
                    exported,
                    eligible,
                    allowance - eligible + charged,
                ),
            )
            inserted += 1
            conn.execute(
                "INSERT INTO year_totals VALUES (?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(tax_year) DO UPDATE SET "
                "grid_charged_kwh=grid_charged_kwh+excluded.grid_charged_kwh, "
                "eligible_export_kwh=eligible_export_kwh+excluded.eligible_export_kwh, "
                "allowance_kwh=excluded.allowance_kwh, "
                "estimated_interval_count=estimated_interval_count+excluded.estimated_interval_count, "
                "interval_count=interval_count+1, flagged_intervals=flagged_intervals+excluded.flagged_intervals",
                (
                    year,
                    charged,
                    eligible,
                    allowance - eligible + charged,
                    int(quality == "hourly_estimate"),
                    int(quality != "measured"),
                ),
            )
            if conn.execute(
                "SELECT 1 FROM intervals WHERE tax_year=? AND start_utc>? LIMIT 1",
                (year, begin.isoformat()),
            ).fetchone():
                replay_years.add(year)
        return inserted, replay_years

    @staticmethod
    def _archive_invalid_intervals(
        conn: sqlite3.Connection,
        overlaps: list[sqlite3.Row],
        start: datetime,
        end: datetime,
        replacement_sample_id: int,
    ) -> None:
        """Keep rejected calculations and evidence when raw history repairs them."""
        for row in overlaps:
            conn.execute(
                "INSERT INTO interval_revisions "
                "(superseded_utc, replacement_sample_id, start_utc, end_utc, tax_year, "
                "sample_id, quality, grid_charge_kwh, battery_export_kwh, eligible_kwh, allowance_kwh) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    replacement_sample_id,
                    *tuple(row),
                ),
            )
            conn.execute("DELETE FROM intervals WHERE start_utc=?", (row["start_utc"],))
            for begin, stop in (
                (datetime.fromisoformat(row["start_utc"]), start),
                (end, datetime.fromisoformat(row["end_utc"])),
            ):
                if begin < stop:
                    conn.execute(
                        "INSERT INTO intervals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (begin.isoformat(), stop.isoformat(), *tuple(row)[2:]),
                    )

    @staticmethod
    def _replay(conn: sqlite3.Connection, year: int) -> None:
        allowance = charged = exported = 0.0
        estimated = flagged = 0
        rows = conn.execute(
            "SELECT * FROM intervals WHERE tax_year=? ORDER BY start_utc", (year,)
        ).fetchall()
        for row in rows:
            eligible = min(allowance, row["battery_export_kwh"])
            allowance += row["grid_charge_kwh"] - eligible
            charged += row["grid_charge_kwh"]
            exported += eligible
            estimated += row["quality"] == "hourly_estimate"
            flagged += row["quality"] != "measured"
            conn.execute(
                "UPDATE intervals SET eligible_kwh=?, allowance_kwh=? WHERE start_utc=?",
                (eligible, allowance, row["start_utc"]),
            )
        conn.execute(
            "UPDATE year_totals SET grid_charged_kwh=?, eligible_export_kwh=?, "
            "allowance_kwh=?, estimated_interval_count=?, interval_count=?, "
            "flagged_intervals=? WHERE tax_year=?",
            (charged, exported, allowance, estimated, len(rows), flagged, year),
        )

    def summary(self, now: datetime) -> dict:
        """Read the selected tax year without carrying a prior year's allowance."""
        year = now.astimezone(STOCKHOLM).year
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM year_totals WHERE tax_year=?", (year,)
            ).fetchone()
            coverage = conn.execute(
                "SELECT (SELECT MIN(start_utc) FROM intervals), "
                "(SELECT MAX(end_utc) FROM intervals)"
            ).fetchone()
            gaps = conn.execute(
                "SELECT COUNT(*) FROM samples WHERE source='restart_gap'"
            ).fetchone()[0]
            return {
                **(
                    dict(row)
                    if row
                    else {
                        "grid_charged_kwh": 0.0,
                        "eligible_export_kwh": 0.0,
                        "allowance_kwh": 0.0,
                        "estimated_interval_count": 0,
                        "interval_count": 0,
                        "flagged_intervals": 0,
                    }
                ),
                "tax_year": year,
                "coverage_start": coverage[0],
                "last_recorded_utc": coverage[1],
                "rule_version": RULE_VERSION,
                "recorded_restart_gaps": gaps,
                "history_import_count": conn.execute(
                    "SELECT COUNT(*) FROM imports"
                ).fetchone()[0],
            }

    def latest_end(self) -> datetime | None:
        with closing(self._connect()) as conn:
            value = conn.execute("SELECT MAX(end_utc) FROM intervals").fetchone()[0]
        return datetime.fromisoformat(value) if value else None

    def log_import(
        self, start: datetime, end: datetime, source: str, inserted: int, skipped: int
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO imports (created_utc, start_utc, end_utc, source, inserted, skipped) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    start.isoformat(),
                    end.isoformat(),
                    source,
                    inserted,
                    skipped,
                ),
            )

    def export_csv(self, directory: Path) -> Path:
        """Export every table from one read transaction to a private CSV file."""
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{uuid4().hex}.csv"
        tables = (
            "configurations",
            "samples",
            "intervals",
            "interval_revisions",
            "year_totals",
            "imports",
        )
        try:
            with closing(self._connect()) as conn, open(
                path,
                "x",
                encoding="utf-8-sig",
                newline="",
                opener=lambda name, flags: os.open(name, flags, 0o600),
            ) as output:
                conn.execute("BEGIN")
                columns = {
                    table: [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                    for table in tables
                }
                fields = list(
                    dict.fromkeys(key for cols in columns.values() for key in cols)
                )
                writer = csv.DictWriter(output, fieldnames=["record_type", *fields])
                writer.writeheader()
                for table in tables:
                    for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                        writer.writerow({"record_type": table, **dict(row)})
        except (OSError, sqlite3.Error):
            path.unlink(missing_ok=True)
            raise
        return path
