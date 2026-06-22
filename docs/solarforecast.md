# Solar Forecast Sensor

## Purpose

An optional sensor that refines an [Open Meteo](https://open-meteo.com/) solar production forecast using your inverter's actual output. Over time it learns how the OM forecast systematically overestimates or underestimates production for your specific installation and conditions, and applies those corrections to future forecasts.

---

## Input

### Required configuration

| Option | Key | Example |
|---|---|---|
| Solar forecast sensor (today) | `forecast_entity` | `sensor.home_energy_production_today` |
| Inverter AC power output sensor | `power_entity` | `sensor.inverter_active_power` |

### Optional configuration

| Option | Key | Example |
|---|---|---|
| Solar forecast sensor (tomorrow) | `forecast_tomorrow_entity` | `sensor.home_energy_production_tomorrow` |

Configure via **Settings → Devices & Services → Electricity Price Levels → Configure**.

The sensor is only created when both `forecast_entity` and `power_entity` are set.
If `forecast_tomorrow_entity` is provided, it must also refer to an existing
entity.

### HA location

The coordinator reads `hass.config.latitude` and `hass.config.longitude` for solar-position calculations. No separate configuration is needed.

---

## Output sensor

**Default entity ID:** `sensor.energy_advisor_solar_forecast` for the first config entry.
Additional entries receive the usual Home Assistant numeric suffixes, such as
`sensor.energy_advisor_solar_forecast_2`.

### State

Corrected power estimate (kW) for the current 15-minute slot — the first forecast entry whose end time is after now.

Unit: `kW` | Device class: `power`

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `forecasts` | list[dict] | 192 entries covering today 00:00 – tomorrow 24:00 (local time), one per 15-minute slot |
| `energy_today_kwh` | float | Total corrected kWh for today (full calendar day) |
| `energy_tomorrow_kwh` | float | Total corrected kWh for tomorrow |
| `total_samples` | int | Number of (OM forecast W, actual inverter W) pairs stored |
| `data_since` | str | ISO date of the oldest stored reading |
| `intraday_scaling` | float | Real-time scaling factor applied to today's remaining slots |

Each entry in `forecasts`:

| Key | Description |
|---|---|
| `end` | Slot end time, local time, `YYYY-MM-DDTHH:MM` (no timezone suffix) |
| `pow` | Corrected forecast power (kW) |
| `raw` | Raw OM forecast power (kW, uncorrected) |

When `exclude_from_recording` is enabled for the integration, the whole sensor
is excluded from recorder/history. Even when recorder is enabled for the
sensor, the large `forecasts` attribute is excluded from recorder attribute
storage.

---

## How it works

### Recording cycle

Every 15 minutes, the coordinator:
1. Collects all inverter power readings buffered during the slot.
2. Computes a time-weighted average (trapezoidal integration).
3. Reads the OM forecast value for that slot.
4. Stores the pair `(om_watts, actual_watts)` in a local SQLite database.

**Recordings are skipped when:**
- No inverter power readings were collected (e.g. HA just started).
- Both OM forecast and inverter reading are below 10 W (night or deep overcast).
- The current export credit price is negative — when credit is negative an inverter may cap output at household consumption to avoid paying to export, understating true solar production and corrupting the correction model.

### Solar-position bins

Readings are grouped by the sun's position in the sky, not by clock time. Position is encoded as `elevation_bin × azimuth_bins + azimuth_bin` using 10° elevation steps and 12 azimuth sectors (30° each). A tree that shades your panels at a specific sun angle is learned once and applied whenever the sun is at that angle, regardless of time of year or DST.

### Correction model

For each solar-position bin the correction factor is an exponentially-weighted average of the historical ratios:

```
correction_factor = Σ(ratio_i × weight_i) / Σ(weight_i)

ratio_i  = actual_W[i] / om_W[i]   (clamped to [0.05, 8.0])
weight_i = exp(−ln(2) / 35 × days_ago_i)
```

- **35-day half-life**: recent weeks matter roughly twice as much as data from five weeks ago.
- **Minimum 5 samples per bin** before its correction factor is applied; until then the factor is 1.0 (no correction).
- **Up to 60 days** of history retained; older rows are purged on startup.

Because correction factors accumulate over days, a single day of data does not meaningfully change tomorrow's forecast. The model improves gradually as history builds across different weather conditions.

### Intraday scaling

Separately from the long-term model, completed daytime slots of the current day are compared to their corrected forecast. If today consistently runs above or below the model (e.g. unexpected cloud cover), a scaling factor is computed and applied to remaining slots of today only. At least 3 completed daytime slots are required before scaling activates; it is clamped to [0.2, 3.0]. Tomorrow's forecast is left unscaled.

---

## Architecture

```
HA state change (power sensor)
    │
    └─► SolarForecastCoordinator._power_buffer   (rolling deque)
              │
              │  every 15 min (async_track_utc_time_change)
              ▼
        _record_current_slot() ─── SQLite DB (solar_forecast_<entry_id>.db)
              │                       readings table: (date, solar_slot, om_w, actual_w)
              ▼
        _refresh()
              │
              ├── _compute_correction_factors_sync()   (weighted aggregate per solar bin)
              ├── _compute_intraday_scaling()
              └── assemble 192-slot forecast list
                        │
                        └─► registered SolarForecastSensor callbacks
                                  │
                                  └─► sensor.energy_advisor_solar_forecast* (HA state update)
```

\* For the first config entry. Later entries receive suffixed entity ids.

**DB location:** `.storage/solar_forecast_<entry_id>.db` (inside HA config dir)

**DB schema version 3** — slot key is solar-position bin (elevation × azimuth).
On upgrade, v2 local-time slots are migrated into solar-position bins, while
v0/v1 data is cleared.

Stale DB files (belonging to removed config entries) are deleted on startup.

---

## Dev mode

When `custom_components/energyadvisor/dev_config.py` defines `HA_TOKEN` and `HA_URL`, the coordinator polls the remote HA REST API for inverter power every 30 seconds instead of listening to a local state change event. This allows development against a live production instance without `remote_homeassistant`.

See `custom_components/energyadvisor/const.py` and the gitignored `custom_components/energyadvisor/dev_config.py` for details.

---

## Continuation notes

- `solar_forecast_coordinator.py` — all logic: DB, correction model, intraday scaling, forecast assembly, dev-mode polling, and export-credit guarding via the main electricity price sensor.
- `sensor/solarforecastsensor.py` — HA sensor wrapper; caches state/attribute summaries and keeps the large `forecasts` payload out of recorder attribute storage.
- `sensor/__init__.py` — conditionally creates `SolarForecastCoordinator` + sensor when both `forecast_entity` and `power_entity` are configured in entry options.
- `config_flow.py` — `solar_forecast` config step (after `thresholds`); options flow includes the same three fields.
- Algorithm constants are at the top of `solar_forecast_coordinator.py`: `MAX_HISTORY_DAYS`, `CORRECTION_HALF_LIFE_DAYS`, `MIN_CORRECTION_SAMPLES`, `SLOT_ELEVATION_STEP`, `SLOT_AZIMUTH_BINS`.
- Tests: `tests/test_solar_forecast_coordinator.py` and `tests/test_solar_forecast_sensor.py`.
