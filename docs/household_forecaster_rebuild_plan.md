# Household Forecaster Rebuild Plan

This is a specification-first rebuild plan for a new household forecaster.
Each step is restart-safe and must remain observable after a Home Assistant restart.

## 0) Central implementation files (tracking map)

Architecture constraint for this plan: **one coordinator + one sensor**.

| File | Role | Expected step coverage |
|---|---|---|
| `custom_components/energyadvisor/coordinators/household_forecast_coordinator.py` | Single forecaster coordinator: scheduling, persistence, aggregation, model, parallax handling, residual correction, fallback logic | Steps 2-9 |
| `custom_components/energyadvisor/sensor/householdforecastsensor.py` | Single HA sensor: state + `forecasts` contract publishing | Steps 1-2, 7, 9 |
| `custom_components/energyadvisor/sensor/__init__.py` | Wire coordinator and sensor entity lifecycle | Steps 1-2 |
| `custom_components/energyadvisor/config_flow_helpers.py` | Household/config schema for required+optional forecast inputs | Steps 3, 7 |
| `custom_components/energyadvisor/config_flow.py` | Config flow + options flow surface for new/updated inputs | Steps 3, 7 |
| `custom_components/energyadvisor/const.py` | Config keys / attribute constants used by coordinator and sensor | Steps 1, 3, 7 |
| `tests/test_household_forecast_sensor.py` | Sensor contract tests (state/format/length/cadence visibility) | Steps 1-2, 9 |
| `tests/test_household_forecast_coordinator.py` | Coordinator behavior tests (aggregation/model/parallax/fallback) | Steps 3-8 |

## 1) Forecast semantics (hard decisions)

### Target definition

- Forecast target is **household base load** (household demand), not net grid import/export.
- Household base load explicitly **excludes** the actively-managed appliance loads: central heating, water heater, dehumidifier, and pool pump. These loads are subtracted from the metered signal before modeling because this integration will control them directly in the future; forecasting them as part of the passive household baseline would be redundant and would fight the integration's own control decisions.
- Output point forecast is named `load`.

### Output unit

- `load` is **kW average over each 15-minute slot** (not kWh/Wh).
- Sensor `state` is the current slot's `load` in kW.

### Input-to-target normalization (energy-delta first)

`power_meter_consumption` is required.

- **Canonical primitive** for modeling and slot aggregation: `delta_kwh` (energy since previous reading).
- **Power is still allowed** as a derived/helper signal for candidate edge detection, event attribution, and diagnostics.

1. Power sensor (`W` or `kW`): for consecutive samples `(t0, p0)` and `(t1, p1)`, compute interval energy by trapezoidal integration:  
   `delta_kwh = ((p0_kw + p1_kw) / 2) * ((t1 - t0)_seconds / 3600)`.
2. Energy counter (`Wh` or `kWh`, monotonic): for consecutive samples `(t0, e0)` and `(t1, e1)`, compute:  
   `delta_kwh = max(0, e1_kwh - e0_kwh)`; negative delta is treated as counter reset and dropped.
3. Allocate each interval `delta_kwh` proportionally into all overlapping 15-minute slots.
4. Compute slot output as: `slot_load_kw = slot_energy_kwh / 0.25`.
5. Derive helper power per interval for event logic:  
   `interval_kw = delta_kwh / interval_hours`.

Short spikes then only affect the slot by their true delivered energy, reducing sensitivity to brief peaks. Any edge threshold below is only for parallax matching and must never be used to discard real appliance load.

If the configured sensor does not represent gross household demand, user must provide a derived/template sensor that does.

### Minimum sample cadence and gap handling

- Preferred median source cadence: <=30 seconds.
- Acceptable median source cadence: <=120 seconds.
- `MAX_INTERVAL_FOR_SPIKES_SEC = 120`: intervals longer than this are not eligible for spike detection/event attribution.
- `MAX_INTERVAL_FOR_TRAINING_SEC = 300`: intervals longer than this are marked `quality=sparse_gap`.
- `HARD_GAP_SEC = 1800`: if no valid required-sensor sample for >=30 minutes, disable short-term residual correction until recovery.

Gap behavior:

1. `interval_sec <= 120`: normal path (eligible for model, spikes, attribution).
2. `120 < interval_sec <= 300`: use for energy aggregation/model, but not for spike detection.
3. `interval_sec > 300`: still allocate energy to preserve totals, but mark sparse and exclude those affected slots from residual-correction fitting.
4. Recovery after hard gap requires 2 consecutive fresh intervals with `interval_sec <= 120` before re-enabling residual correction.

### Source ingestion policy (explicit)

- Ingestion mode is **both** event-driven and polled.
- Event-driven path: subscribe to HA state-changed events for required/optional entities.
- Poll path: watchdog poll every 60 seconds for required entities (and every 120 seconds for optional entities).
- Poll samples are recorded when value changed, or when last stored sample age exceeds 120 seconds (heartbeat sample).
- If event and poll produce the same timestamped value, deduplicate into one sample and mark source priority as `event > poll`.

## 2) Output contract and slot math

The output array format is fixed:

```json
[
  { "from": "2026-09-05T06:00", "load": 0.62 },
  { "from": "2026-09-05T06:15", "load": 0.58 }
]
```

- `from`: local wall clock, format `YYYY-MM-DDTHH:MM` (no timezone suffix).
- Array length: **192 slots** (48h at 15-minute resolution).
- Array attribute name: `forecasts`.
- Sensor state value: the **current in-progress slot's** `load` (the slot whose `from` covers the current local time), looked up inside the `forecasts` array. This is *not* always `forecasts[0]` (see slot anchoring below).

### Slot anchoring

- The `forecasts` array is anchored to **local midnight** of the current day: slot 0 always starts at `00:00` local time, slot 191 starts at `00:00 + 47h45m`.
- Once a slot has been published, its `load` value is **frozen** and must not change on subsequent refreshes, even after the slot's start time is in the past. Only the current and future slots may be recomputed on each refresh.
- Rationale: freezing already-published (past) slots keeps the visualized forecast curve stable across a full day, so users can see how earlier predictions compared to what actually happened without the chart retroactively rewriting history.
- The sensor `state` is derived separately from the array by locating the slot matching the current in-progress 15-minute period; it is not simply the first array element.

### Update cadence

- Full 192-slot forecast recomputed every 15 minutes at `:00`, `:15`, `:30`, `:45`; only the current and future slots are rewritten, past slots are carried forward unchanged.
- Manual refresh may happen between boundaries; it still only updates the current and future slots, never the frozen past slots.

### DST behavior

- Internal calculations use timezone-aware datetimes.
- Output formatting drops timezone suffix for compatibility.
- Horizon is always 192 physical 15-minute intervals (48 real hours):
  - spring-forward: missing local hour is skipped in `from` values.
  - fall-back: repeated local hour may produce duplicate `from` strings.
- When duplicate `from` exists (fall-back), array order is authoritative.

### Diagnostics attributes contract

Diagnostics live on the same HA sensor as extra state attributes:

1. `reason` (string, required): human-readable status/fallback explanation.
2. `quality_warnings` (list[string], required): machine-readable warning codes; empty list when healthy.
3. `quality_status` (string, required): one of `ok`, `degraded`, `stale`, `fallback`.
4. `last_valid_required_sample` (string or null): local `YYYY-MM-DDTHH:MM` timestamp of latest valid required sample.
5. `last_forecast_generation` (string): local `YYYY-MM-DDTHH:MM` timestamp of current forecast generation.

## 3) Sensor contract (required, optional, precedence, units)

### Required
All incoming sensors should be evaluated on data quality and cadence. If any required sensor is missing or invalid, the forecast must still publish a full 192-slot array with a valid state, but the `reason` attribute must indicate the problem.
If a sensor is deemed not good enough, ask the user for a replacement sensor.

| Key | Expected type | Accepted units/states | Purpose |
|---|---|---|---|
| `household_energy_usage` | sensor | `W`, `kW`, `Wh`, or `kWh` | Main target signal |

### Optional from current configuration

| Key | Expected type | Units/states | Role |
|---|---|---|---|
| `outdoor_temperature_entity` | sensor | `C`/`F` (normalized to C) | Weather sensitivity |
| `water_heater_active_entity` | binary/switch/sensor | active/inactive mapping | Event feature |
| `central_heating_active_entity` | binary/switch/sensor | active/inactive mapping | Event feature |
| `central_heating_power_entity` | sensor | `W`/`kW` | Primary event magnitude when available |
| `central_heating_power_w` | numeric | watts | Fallback magnitude when the live power sensor is missing |
| `grid_import_entity` | sensor | `W`/`kW`/energy counter | Context/diagnostic |
| `grid_export_entity` | sensor | `W`/`kW`/energy counter | Context/diagnostic |
| `battery_soc_entity` | sensor | `%` | Storage context |
| `battery_charge_power_entity` | sensor | signed/unsigned `W`/`kW` | Storage context |
| `water_heater_power_entity` | sensor | `W`/`kW` | Primary event magnitude when available |
| `water_heater_power_w` | numeric | watts | Fallback magnitude when the live power sensor is missing |
| `bathroom_humidity_entity` | sensor | `%` | Hot-water proxy |
| `pool_pump_power_entity` | sensor | `W`/`kW` | Flexible load context |
| `pool_pump_power_w` | numeric | watts | Fallback magnitude |
| `dehumidifier_power_entity` | sensor | `W`/`kW` | Flexible load context |
| `dehumidifier_power_w` | numeric | watts | Fallback magnitude |

### Additional new keys (recommended)

1. `occupancy_entity`
2. `indoor_temperature_entity`
3. `ev_charger_power_entity` and/or `ev_charger_active_entity`
4. `weather_forecast_entity`
5. `dynamic_price_entity` (optional behavior feature)
6. `holiday_calendar_entity` (deferred to a future release; see Section 5 day-type handling)

### Precedence rules

1. Live entity value beats fallback config value.
2. Fallback numeric values are used only when the corresponding live sensor is missing/unavailable.
3. Unknown/unavailable/non-numeric samples are ignored, never silently coerced to zero.
4. Optional sensor failure disables only that feature; forecast output must still publish.
5. When both an appliance active-state signal and a measured power signal are available, the measured power signal takes precedence for load magnitude. The active-state signal is used for gating, timing, and explanation, not for overriding real measured draw.

### Active/inactive mapping

- `binary_sensor`, `switch`, `input_boolean`: active when state is `on`.
- Numeric sensor used as boolean proxy: active when value `> 0`.
- String sensor proxy: active values allowed: `on`, `true`, `active`, `heating`, `home` (case-insensitive).

## 4) Persistence shape and restart safety

### Storage

- SQLite file: `.storage/energyadvisor_household_forecast_<entry_id>.db`

### Tables (minimum schema)

1. `raw_samples(ts_utc, sensor_key, value, unit, quality)`  
   Primary key: `(ts_utc, sensor_key)`
2. `interval_energy(ts_from_utc, ts_to_utc, sensor_key, delta_kwh, interval_sec, interval_kw, quality)`  
   Primary key: `(ts_from_utc, sensor_key)`
3. `raw_events(ts_utc, event_key, state_num, state_text, quality)`  
   Primary key: `(ts_utc, event_key)`
4. `slot_rows(slot_start_utc, slot_energy_kwh, load_kw, sample_count, quality_score, features_json)`  
   Primary key: `slot_start_utc`
5. `forecast_runs(generated_at_utc, slot_start_utc, load_kw)`  
   Primary key: `(generated_at_utc, slot_start_utc)`
6. `meta(key, value)`  
   Primary key: `key`

`slot_rows` field contracts:

- `quality_score`: float in `[0.0, 1.0]`, where `1.0` means no missing required data and no sparse-gap penalties for the slot.
- `features_json` minimal schema:
  ```json
  {
    "slot_index": 0,
    "day_type": "workday",
    "is_holiday": false,
    "interval_count": 12,
    "sparse_interval_count": 0,
    "required_missing": false,
    "outdoor_temp_c": 15.2,
    "event_flags": {
      "water_heater_active": 0,
      "central_heating_active": 1
    }
  }
  ```

### Retention

- `raw_samples` + `interval_energy` + `raw_events`: 21 days
- `slot_rows`: 180 days
- `forecast_runs`: 14 days (enough for diagnostics and rollback comparison)
- prune daily

### Checkpointing and restart flow

At each 15-minute cycle, in one transaction:

1. convert new raw meter samples into `interval_energy` rows,
2. finalize the last closed slot into `slot_rows` from interval energy (idempotent upsert),
3. write full 192-slot forecast into `forecast_runs`,
4. update `meta.last_finalized_slot_utc` and `meta.last_generation_utc`,
5. commit.

On startup:

1. read latest forecast from `forecast_runs` and publish immediately,
2. replay any missing closed slots from `last_finalized_slot_utc` to now,
3. regenerate and republish current 192-slot forecast.

## 5) Model criteria (hard decisions)

### Allowed model family (v1)

- Statistical model only (no heavy external ML dependency required):
  - seasonal profile by slot-of-day and day-type,
  - recency weighting (exponential decay),
  - short-term trend blend from recent slots.

### Day-type and holiday handling

- `day_type` categories for v1: `workday`, `saturday`, `sunday`.
- Mapping:
  - Monday-Friday -> `workday`
  - Saturday -> `saturday`
  - Sunday -> `sunday`
- Model keeps separate seasonal profiles per `(day_type, slot_index)`.
- **Deferred to a future release:** explicit `holiday` day-type support driven by a `holiday_calendar_entity` (with fallback to the `sunday` profile when holiday history is thin). Not required for v1; do not block acceptance on it.

### Training windows

- minimum history to enable learned profile: 7 days
- preferred training window: last 56 days
- hard maximum history used for fitting: 180 days

### Retrain cadence

- incremental update every closed slot (15 minutes); each refresh refits from retained slot history, so there is no separate daily full-rebuild job.
- no legacy fixed-hour "quiet window" (e.g. 01:00/04:00) sampling job is used; all retained samples are captured continuously via the event/poll ingestion policy in Section 1.

### "Good enough" thresholds

Measured on rolling 30-day backtest over closed slots:

1. MAE <= 0.35 kW
2. absolute bias <= 0.10 kW
3. MAE at least 10% better than naive baseline (same slot previous day)

## 6) Concrete definitions for Step 6 and Step 8 topics

### Parallax handling (30s lag) - concrete defaults

- Rising edge threshold (`SPIKE_THRESHOLD_W`): 700 W above the recent baseline; used only to identify candidate event edges, not to suppress legitimate appliance load
- Match-back window (`MATCH_BACK_SEC`): 45 s
- Confirmation timeout (`CONFIRM_TIMEOUT_SEC`): 90 s
- Spike detection source: interval-derived power estimate  
  `interval_kw = delta_kwh / interval_hours` (not single-sample instantaneous power)
- Spike detection only runs on intervals where `interval_sec <= MAX_INTERVAL_FOR_SPIKES_SEC`.

Algorithm:

1. Detect meter spikes above threshold and create provisional events.
2. If device-active signal appears within timeout, match backward to nearest unmatched spike within 45 s.
3. Use spike timestamp as effective event start.
4. If not confirmed, keep as unknown transient event.

If the configured inputs are already energy counters and you do not need lagged active-state attribution, this step can be skipped entirely.

### Online residual correction - concrete defaults

- EMA alpha (`RESIDUAL_ALPHA`): 0.35
- Activation threshold: absolute slot error >= 0.25 kW for 2 consecutive closed slots
- Max correction clamp: +/-1.5 kW
- Apply horizon: next 8 slots with per-slot decay factor 0.82

Correction is always on after warm-up and quality checks; if checks fail, correction contribution is set to 0.

## 7) Fallback behavior

| Scenario | Behavior | Required output |
|---|---|---|
| Cold start (<24 finalized slots) | Publish constant profile 0.60 kW | 192 slots + valid state + reason |
| Warm-up (24 slots to <7 days) | Use simple recency profile, no advanced context | 192 slots + valid state |
| Required sensor outage <30 min | Keep latest forecast, mark stale | 192 slots + valid state + reason |
| Required sensor outage >=30 min | Disable short-term correction, use seasonal baseline from stored history | 192 slots + valid state + reason |
| Sparse sampling (intervals >300 s) | Keep energy aggregation, disable spike-sensitive logic on sparse intervals, quality-flag affected slots | 192 slots + valid state + reason |
| Optional sensor outage | Disable only affected feature | 192 slots + valid state |
| Invalid sample (NaN/unit mismatch/reset delta) | Drop sample, increment quality warning | 192 slots + valid state |
| No usable history + missing live data | Constant 0.60 kW hard fallback | 192 slots + valid state + reason |

## 8) Step-by-step implementation with restart checkpoints

| Step | Build in this step | Restart-visible result |
|---|---|---|
| 1 | Contract shell (`state` + `forecasts[192]` with `from`/`load`) | Correct format and lengths |
| 2 | 15-minute scheduler + full horizon refresh | Updates on quarter-hour boundaries |
| 3 | Raw capture + DB schema + retention jobs | Data persists across restart |
| 4 | Energy-delta aggregation + quality scoring (`interval_energy -> slot_energy_kwh -> load_kw`) | Non-placeholder slot rows produced |
| 5 | Baseline statistical model + retrain cadence | Time-varying forecast from history |
| 6 | Parallax matcher with fixed thresholds | Delayed activations correctly attributed |
| 7 | Context features + precedence handling | Better forecast with optional sensors, stable without |
| 8 | Residual correction with clamps/decay | Faster adaptation to sudden shifts |
| 9 | Guardrails + fallback modes + diagnostics | Stable degraded behavior under faults |

## 9) Step handover summaries (fill during implementation)

Purpose: after each step is implemented, fill that step's summary before ending the session.
The next Copilot session should start by reading the latest filled summary.

### Step 1 summary - Contract shell

- Status: `done`
- Files changed: `custom_components/energyadvisor/coordinators/household_forecast_coordinator.py`, `custom_components/energyadvisor/sensor/householdforecastsensor.py`, `tests/test_household_forecast_sensor.py`, `README.md`, `old/householdforecast.md`
- What was implemented: The household forecast sensor now exposes a 192-slot `forecasts` array using local `from`/`load` entries anchored to local midnight, while keeping the existing static 0.5 kW shell in place.
- Output contract check (state + 192 slots + from/load format): `state` stays at 0.5 kW; `forecasts` length is 192; every slot has only `from` and `load`, and historical slots remain fixed when the shell refreshes.
- Handover to Step 2: add the scheduler heartbeat so the forecast shell can refresh quickly without shifting the midnight anchor.

### Step 2 summary - 15-minute scheduler

- Status: `done`
- Files changed: `custom_components/energyadvisor/coordinators/household_forecast_coordinator.py`, `tests/test_household_forecast_sensor.py`, `README.md`, `old/householdforecast.md`
- What was implemented: Added a quarter-hour heartbeat that triggers the same forecast refresh callback, so the 192-slot shell is republished on `:00`, `:15`, `:30`, and `:45` while keeping the midnight anchor fixed. A `last_forecast_generation` attribute now changes on each tick so HA surfaces the update.
- Quarter-hour refresh behavior observed: The coordinator now registers a dedicated refresh listener and the sensor updates through the shared update callback path while previously published slots stay fixed. The generation timestamp advances on each tick, so HA can show the refresh even when the forecast values are unchanged.
- Handover to Step 3: add raw sample capture and persistence schema so refreshes can survive restart with real history instead of the static shell.

### Step 3 summary - Raw capture and DB schema

- Status: `done`
- Files changed: `custom_components/energyadvisor/coordinators/household_forecast_coordinator.py`, `tests/test_household_forecast_coordinator.py`, `tests/test_household_forecast_sensor.py`, `README.md`, `old/householdforecast.md`
- What was implemented: The coordinator now opens `.storage/energyadvisor_household_forecast_<entry_id>.db`, creates the planned raw capture schema, captures raw numeric samples and quiet-night event rows for the configured household entities, and reloads the latest persisted rows on restart.
- DB schema/retention/checkpoint notes: The SQLite file now includes `raw_samples`, `interval_energy`, `raw_events`, `slot_rows`, `forecast_runs`, and `meta`; raw rows are pruned on a daily heartbeat with 21-day retention, `slot_rows` retain 180 days, and `forecast_runs` retain 14 days.
- Handover to Step 4: use the persisted raw meter history to convert interval deltas into `interval_energy` and then into slot-level load rows.

### Step 4 summary - Energy-delta aggregation

- Status: `done`
- Files changed: `custom_components/energyadvisor/coordinators/household_forecast_coordinator.py`, `tests/test_household_forecast_coordinator.py`, `docs/household_forecaster_rebuild_plan.md`
- What was implemented: The coordinator now rebuilds `interval_energy` from retained raw meter samples, allocates interval energy into closed 15-minute `slot_rows`, persists 192-slot `forecast_runs` when history exists, and stores `last_finalized_slot_utc` / `last_generation_utc` checkpoints.
- Interval/cadence/gap handling outcome: Power and energy-counter samples are normalized to kW/kWh before aggregation; intervals longer than 300 s are marked `sparse_gap`, and the slot quality score is reduced when sparse intervals contribute to a slot.
- Handover to Step 5: Fit the statistical baseline on top of the persisted slot rows while keeping the new interval-energy checkpoint flow intact.

### Step 5 summary - Baseline statistical model

- Status: `done`
- Files changed: `custom_components/energyadvisor/coordinators/household_forecast_coordinator.py`, `tests/test_household_forecast_coordinator.py`, `old/householdforecast.md`, `README.md`
- What was implemented: The coordinator now fits a seasonal baseline from retained slot rows, with recency weighting and a short-term trend blend, then persists the learned 192-slot forecast into `forecast_runs`. Cold start still publishes a full forecast shell so the sensor never goes empty.
- Retrain/history window notes: The learned model uses retained slot history up to 180 days, prefers the latest 56 days for fitting, and switches into the learned path once at least 7 distinct learning days are available.
- Handover to Step 6: add the parallax matcher so delayed active-state transitions can be attributed to the correct meter spike before the next context step.

### Step 6 summary - Parallax matcher

- Status: `Skipped - not needed for this household forecaster`
- Files changed: `TBD`
- What was implemented: `TBD`
- Match-rate/alignment metrics: `TBD`
- Handover to Step 7: `TBD`

### Step 7 summary - Context features and precedence

- Status: `done`
- Files changed: `custom_components/energyadvisor/coordinators/household_forecast_coordinator.py`, `custom_components/energyadvisor/sensor/__init__.py`, `custom_components/energyadvisor/config_flow.py`, `custom_components/energyadvisor/config_flow_helpers.py`, `custom_components/energyadvisor/const.py`, `tests/test_household_forecast_coordinator.py`, `tests/test_config_flow.py`, `README.md`, `old/householdforecast.md`
- What was implemented: The household forecast now starts from the household meter alone, keeps optional appliance sensors available, and subtracts the measured water-heater, central-heating, pool-pump, and dehumidifier loads before fitting the seasonal model. Live appliance power sensors take precedence over fallback watt values, and the docs/config flow now expose central-heating power alongside the other subtractable loads.
- Optional-sensor degradation behavior: Missing optional sensors no longer block the forecast; the subtraction for that appliance simply drops out and the sensor keeps publishing the full 192-slot baseline.
- Handover to Step 8: add residual correction on top of the net household base load.

### Step 8 summary - Residual correction

- Status: `done`
- Files changed: `custom_components/energyadvisor/coordinators/household_forecast_coordinator.py`, `tests/test_household_forecast_coordinator.py`, `README.md`, `old/householdforecast.md`
- What was implemented: Added an online residual layer on top of the learned household baseline. The coordinator now looks at the most recent closed slots from the current day, compares them against the previously published forecast, and nudges the next few slots when the last two errors stay above the activation threshold.
- Error-improvement and clamp behavior: The correction is EMA-smoothed with `RESIDUAL_ALPHA = 0.35`, decays by `0.82` across the next 8 slots, and is clamped to +/-1.5 kW so sudden step changes adapt faster without runaway adjustments.
- Handover to Step 9: add guardrails, quality flags, and diagnostics for stale data and degraded sensor inputs.

### Step 9 summary - Guardrails and diagnostics

- Status: `done`
- Files changed: `custom_components/energyadvisor/coordinators/household_forecast_coordinator.py`, `custom_components/energyadvisor/sensor/householdforecastsensor.py`, `tests/test_household_forecast_coordinator.py`, `tests/test_household_forecast_sensor.py`, `README.md`, `old/householdforecast.md`
- What was implemented: Added sensor-facing diagnostics for `quality_status`, `quality_warnings`, and `last_valid_required_sample`, plus coordinator guardrails for stale required-meter samples and retained warnings for dropped invalid samples. The forecast still publishes a full 192-slot contract, but the reason string now explains stale/degraded/fallback states when the required meter is late or unavailable.
- Fallback/reliability outcomes: Healthy runs report `quality_status=ok` with an empty warning list; short required-meter outages surface as `stale`; longer outages and sparse/invalid input surface as `degraded`; cold start stays on the fixed 0.60 kW fallback profile.
- Final handover/remaining work: Step 9 is complete. The acceptance checks now have a diagnostic surface to verify against during restart and fault-injection tests.

## 10) Acceptance tests (checkable)

| Area | Pass condition |
|---|---|
| Contract | `forecasts` length is 192; all `from` match regex `^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}$`; state equals the current in-progress slot's `load` |
| Slot grid | Adjacent slot start times differ by 15 minutes in local wall clock ordering (allow DST jump/overlap behavior); slot 0 is local midnight and previously-published (past) slots are byte-for-byte unchanged across refreshes |
| Diagnostics surface | Sensor always exposes `reason`, `quality_status`, `quality_warnings`; `quality_warnings` is a list and empty in healthy baseline runs |
| Ingestion policy | Under normal updates, event-driven ingestion is primary and watchdog polling backfills heartbeat so required-sensor sample age never exceeds 120 s |
| Step 6 parallax | Skipped; not applicable |
| Step 7 feature resilience | Remove any one optional sensor: no crash; still 192 slots and valid state within one update cycle |
| Step 8 residual correction | In step-change test (+2 kW for 2h), next-4-slot MAE improves >=20% vs correction-off baseline; correction never exceeds +/-1.5 kW |
| Energy-delta refinement | Two scenarios with equal delivered energy in a slot (flat vs short spike) produce slot `load` difference <=0.02 kW |
| Cadence and gap handling | Intervals >120 s produce no spike events; intervals >300 s are quality-flagged sparse; after >=30 min gap, correction resumes only after 2 fresh intervals <=120 s |
| Day-type and holiday | Deferred to a future release (see Section 5); v1 only needs to verify weekday/weekend mapping (`workday`/`saturday`/`sunday`) is correct |
| Reliability | Restart mid-slot: sensor publishes persisted forecast first, then refreshed forecast; no empty output and no missing 15-minute cadence |
| Quality gate | Rolling 30-day MAE <=0.35 kW and >=10% better MAE than naive previous-day-slot baseline |
