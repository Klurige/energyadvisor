# Solar Forecast Sensor

## Purpose

An optional sensor that refines a raw solar production forecast (any sensor that
exposes a 15-minute forecast payload, for example Open-Meteo-style `watts` /
`wh_period` data or a Solcast-style `forecasts` list) using your solar's actual
output. Over time it learns how the raw forecast systematically overestimates or
underestimates production for your specific installation and conditions, and
applies those corrections to future forecasts. Each 15-minute point is adjusted
in two steps: first by a learned correction factor for the current
solar-position bin (sun elevation + azimuth), then (for today's remaining slots
only) by an intraday scaling factor based on how actual production has tracked
so far today.

---

## Input

### Configuration

All three keys are stored in the config entry options and are optional in the
UI, but `forecast_entity` and `power_entity` must be set together (or both left
empty).

| Option (UI label) | Key | Required | Example |
|---|---|---|---|
| Solar forecast sensor (today) | `forecast_entity` | Yes, together with `power_entity` | `sensor.home_energy_production_today` |
| Solar actual power sensor (W) | `power_entity` | Yes, together with `forecast_entity` | `sensor.solar_active_power` |
| Solar forecast sensor (tomorrow) – optional | `forecast_tomorrow_entity` | No | `sensor.home_energy_production_tomorrow` |

Configure via **Settings → Devices & Services → Energy Advisor → Configure**:
the options flow reaches the **Solar forecast** step (`solar_forecast`) after
*Taxes and VAT* (first page with the price sensor and thresholds → supplier fees
and credits → grid fees and credits → taxes and VAT → **solar forecast** →
battery …). During initial setup the same step follows *thresholds* and
precedes *battery*. Saving options reloads the config entry.

The sensor is only created when both `forecast_entity` and `power_entity` are set;
`forecast_tomorrow_entity` on its own has no effect. All three selectors are
limited to the `sensor` domain.

Validation in the config flow (`_validate_solar_forecast_entities` in
`config_flow_helpers.py`):

- Setting only one of `forecast_entity` / `power_entity` fails with
  `solar_entity_required` ("Both forecast sensor and power sensor must be
  provided together.").
- Every entity that is set must exist, otherwise `entity_not_found`.
- `forecast_entity` and `forecast_tomorrow_entity` must additionally be
  available (state not `unknown`/`unavailable`) and expose a non-empty `watts`,
  `wh_period`, or `forecasts` attribute; energy-only summary sensors are
  rejected with `invalid_sensor`.
- `power_entity` is only checked for existence.

The coordinator watches both forecast sensors and rebuilds the forecast when
either one changes.

### Forecast payload formats

For each configured forecast entity (today first, then tomorrow; entities that
are missing or `unknown`/`unavailable` are skipped) the coordinator reads these
attributes, collecting points from local midnight today until 49 h after it. Later
formats overwrite earlier ones at the same timestamp. Naive timestamps are
treated as local time.

| Attribute | Format | Conversion to W |
|---|---|---|
| `wh_period` | dict `{timestamp: Wh per 15 min}` | `Wh × 4` |
| `watts` (or `Watts`) | dict `{timestamp: W}` | none |
| `forecasts` | list of dicts with `period_end` (or `period_start` / `datetime`) and `pv_estimate` (or `pv_estimate_mean`) in kW | `kW × 1000` |

The solar power sensor (`power_entity`) must report watts as a numeric state;
`unknown`/`unavailable`/non-numeric states are ignored.

### HA location

The coordinator reads `hass.config.latitude`, `hass.config.longitude` and
`hass.config.time_zone` for solar-position and local-day calculations. No
separate configuration is needed.

---

## Output sensor

**Default entity ID:** `sensor.energy_advisor_solar_forecast` for the first config entry
(entity name "Solar Forecast"). Additional entries receive the usual Home
Assistant numeric suffixes, such as `sensor.energy_advisor_solar_forecast_2`.

### State

Corrected power estimate (kW, rounded to 3 decimals) for the current 15-minute
slot — the `pow` of the first `forecasts` entry whose `end` is after now. The
scan only stops at a non-zero value, so when that entry is 0 (night) the first
later entry with non-zero `pow` is reported instead (0 if there is none).
Each `pow` is `raw forecast × solar-position correction × intraday scaling`
(intraday scaling applies to today's future slots only; slots whose raw forecast
is below 10 W are 0). State and attributes are recomputed whenever the
coordinator publishes a changed forecast (15-minute tick or forecast entity
update); the state is `0.0` until the first forecast is available.

Unit: `kW` | Device class: `power`

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `forecasts` | list[dict] | 192 entries, one per 15-minute slot, from today 00:15 (first `end`) to 00:00 the day after tomorrow (last `end`); empty list when no forecast data is available |
| `energy_today_kwh` | float | Σ `pow × 0.25` over entries whose `end` date is today, rounded to 2 decimals |
| `energy_tomorrow_kwh` | float | Σ `pow × 0.25` over entries whose `end` date is after today, rounded to 2 decimals |
| `total_samples` | int | Number of stored readings (raw forecast W, actual solar W) used by the correction model: daytime rows with raw forecast ≥ 10 W from the last 60 days |
| `data_since` | str \| None | ISO date of the oldest of those readings; `None` when there are none |
| `intraday_scaling` | float | Scaling factor applied to today's remaining slots, rounded to 3 decimals (`1.0` = none) |

Each entry in `forecasts`:

| Key | Description |
|---|---|
| `end` | Slot end time, local time, `YYYY-MM-DDTHH:MM` (no timezone suffix) |
| `pow` | Corrected forecast power (kW, 4 decimals) |
| `raw` | Raw forecast power (kW, 4 decimals, uncorrected) |

The `forecasts` attribute is always excluded from recorder attribute storage
(`_unrecorded_attributes`). The sensor also copies the entry option
`exclude_from_recording` (default `True`; set to `True` when the config entry is
created, no field in the UI) into `_attr_exclude_from_recording`; neither the
integration nor Home Assistant core reads that attribute, so the option does not
change recorder behaviour.

---

## How it works

### Recording cycle

Every numeric state change of `power_entity` is appended to a rolling buffer
(readings older than 20 minutes are dropped at each tick). At
`:00:30`, `:15:30`, `:30:30` and `:45:30` UTC the coordinator:
1. Computes the time-weighted average power of the last 15 minutes
   (trapezoidal integration, first/last value extended to the slot edges; a
   single reading is used as-is, and if no reading fell inside the window the
   latest buffered reading is used).
2. Reads the raw forecast value nearest to the tick time (within 10 minutes,
   otherwise 0 W).
3. Stores the pair `(raw forecast W, actual W)` in the local SQLite database
   under the current solar-position bin, keyed by local calendar date.
4. Purges rows older than 60 days, recomputes the correction factors and
   intraday scaling, and rebuilds the forecast.

A forecast entity update triggers the same recompute and rebuild without
recording a reading (at most one such refresh runs at a time). After setup, a
single retry runs 30 seconds later if the forecast is still empty.

**Recordings are skipped when:**
- The power buffer is empty (e.g. HA just started).
- Both the raw forecast value and the solar reading are below 10 W (night or deep overcast).
- The current export credit (`current_credit` of the price sensor, `PriceSensor` in `sensor/price.py`) is negative — solar may then cap output at household consumption to avoid paying to export, understating true solar production and corrupting the correction model. No price sensor or a non-numeric credit never skips.
- The sun is below the horizon (no solar-position bin).

There is one row per (local date, solar-position bin): a later reading in the
same bin on the same day replaces the earlier one. Values are rounded to two
decimals.

### Solar-position bins

Readings are grouped by the sun's position in the sky, not by clock time. The
position is computed with a pure-Python Spencer/Iqbal implementation from the
HA latitude/longitude and encoded as `elevation_bin × 12 + azimuth_bin`, using
10° elevation steps (9 bins, 0–90°) and 12 azimuth sectors (30° each), giving
bins `0–107`. Below the horizon the bin is `-1` and is neither recorded nor
corrected. A tree that shades your panels at a specific sun angle is learned
once and applied whenever the sun is at that angle, regardless of time of year
or DST.

### Correction model

For each solar-position bin the correction factor is an exponentially-weighted average of the historical ratios:

```
correction_factor = Σ(ratio_i × weight_i) / Σ(weight_i)

ratio_i  = actual_W[i] / raw_W[i]   (clamped to [0.05, 8.0])
weight_i = exp(−ln(2) / 35 × days_ago_i)
```

- **35-day half-life** (`CORRECTION_HALF_LIFE_DAYS`): recent weeks matter roughly twice as much as data from five weeks ago.
- **Minimum 5 samples per bin** (`MIN_CORRECTION_SAMPLES`, i.e. 5 distinct days) before its correction factor is applied; until then the factor is 1.0 (no correction).
- **Up to 60 days** of history (`MAX_HISTORY_DAYS`): older rows are ignored by the model and deleted on every 15-minute recording tick.
- Only rows with a raw forecast of at least 10 W (`NIGHT_THRESHOLD_W`) enter the model.

Because correction factors accumulate over days, a single day of data does not meaningfully change tomorrow's forecast. The model improves gradually as history builds across different weather conditions.

### Intraday scaling

Separately from the long-term model, today's stored readings (rows for the current date with a raw forecast of at least 10 W, one per solar-position bin) are compared to their corrected forecast: `scaling = mean(actual_W / (raw_W × correction_factor))`, with factor 1.0 for bins without a correction yet. If today consistently runs above or below the model (e.g. unexpected cloud cover), the scaling is applied to the remaining slots of today only (slots ending after now on today's local date). At least 3 such rows (`INTRADAY_MIN_SAMPLES`) are required before scaling activates (otherwise 1.0); it is clamped to [0.2, 3.0] (`INTRADAY_MIN_SCALING`, `INTRADAY_MAX_SCALING`). Tomorrow's forecast is left unscaled.

### Forecast assembly

For each of the 192 slots (96 per day, from local midnight today) the raw forecast value is the point nearest to the slot end (within 20 minutes, otherwise 0 W). If it is at least 10 W, `pow = raw × correction_factor(bin of slot end)`, multiplied by the intraday scaling for today's future slots; below 10 W, `pow` is 0. If no forecast data can be read, the forecast is empty.

---

## Architecture

```
HA state change (power_entity)
    │
    └─► SolarForecastCoordinator._power_buffer   (rolling deque)
              │
              │  :00:30 / :15:30 / :30:30 / :45:30 UTC (async_track_utc_time_change)
              ▼
        _refresh(now)  ◄── forecast entity state change: _refresh() (no recording)
              │
              ├── _record_current_slot() ─── SQLite DB (solar_forecast_<entry_id>.db)
              │                                 readings table: (date, slot, om_w, actual_w)
              ├── _purge_old_data_sync()
              ├── _compute_correction_factors_sync()   (weighted aggregate per solar bin)
              ├── _compute_intraday_scaling_sync()
              └── _build_forecast_from_data()          (192-slot forecast list)
                        │
                        └─► registered SolarForecastSensor callbacks (only if the forecast changed)
                                  │
                                  └─► sensor.energy_advisor_solar_forecast* (HA state update)
```

\* For the first config entry. Later entries receive suffixed entity ids.

**DB location:** `<HA config dir>/.storage/solar_forecast_<entry_id>.db` (SQLite, WAL mode)

**Table:** `readings(date TEXT, slot INTEGER, om_w REAL, actual_w REAL, PRIMARY KEY (date, slot))` plus index `idx_slot` on `slot`. `date` is the local calendar date, `slot` the solar-position bin, `om_w` the raw forecast watts and `actual_w` the 15-minute average solar watts.

**DB schema version 3** (`PRAGMA user_version`) — slot key is solar-position bin (elevation × azimuth).
On upgrade, v2 local-time slots are migrated into solar-position bins (night rows dropped, rows landing in the same bin on the same date averaged), while
v0/v1 data is cleared.

Stale DB files (`solar_forecast_<id>.db` plus `-wal`/`-shm` files whose `<id>` does not belong to an existing Energy Advisor config entry) are deleted when the coordinator is set up.

---

## Dev mode

When the gitignored `custom_components/energyadvisor/dev_config.py` defines `HA_TOKEN` and `HA_URL` and `DEV_DEFAULTS["power_entity"]`, the coordinator polls the remote HA REST API (`<HA_URL>/api/states/<DEV_DEFAULTS["power_entity"]>`) for solar power every 30 seconds instead of listening to state changes of the configured `power_entity`. This allows development against a live production instance without `remote_homeassistant`. The configured `power_entity` must still be set for the sensor to be created.

The config flow also pre-fills its fields from `DEV_DEFAULTS` when `DEV_DEFAULTS_ENABLED` is true; see [`const.py`](../custom_components/energyadvisor/const.py).

---

## Continuation notes

- `coordinators/solar_forecast_coordinator.py` — all logic: DB, correction model, intraday scaling, forecast assembly, dev-mode polling, and export-credit guarding via the price sensor (`PriceSensor` in `sensor/price.py`, passed to the coordinator as `levels_sensor`).
- `sensor/solarforecastsensor.py` — HA sensor wrapper; caches state/attribute summaries and keeps the large `forecasts` payload out of recorder attribute storage.
- `sensor/__init__.py` — conditionally creates `SolarForecastCoordinator` + sensor when both `forecast_entity` and `power_entity` are configured in entry options.
- `config_flow.py` / `config_flow_helpers.py` — `solar_forecast` step (after `thresholds` in the setup flow, after `taxes_and_vat` in the options flow) and `_validate_solar_forecast_entities`; labels live in `strings.json` and `translations/en.json`.
- Algorithm constants are at the top of `coordinators/solar_forecast_coordinator.py`: `MAX_HISTORY_DAYS`, `CORRECTION_HALF_LIFE_DAYS`, `MIN_CORRECTION_SAMPLES`, `NIGHT_THRESHOLD_W`, `MIN_RATIO`, `MAX_RATIO`, `INTRADAY_MIN_SAMPLES`, `INTRADAY_MIN_SCALING`, `INTRADAY_MAX_SCALING`, `DB_SCHEMA_VERSION`, `SLOT_ELEVATION_STEP`, `SLOT_AZIMUTH_BINS`.
- Tests: [`tests/test_solar_forecast_coordinator.py`](../tests/test_solar_forecast_coordinator.py) and [`tests/test_solar_forecast_sensor.py`](../tests/test_solar_forecast_sensor.py).
