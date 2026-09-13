# Battery Charge Mode Optimiser Plan

This document is the implementation-ready specification for the Home Assistant battery charge mode optimiser. It supersedes any legacy notes in `docs/old`; those files are deprecated and must not be used as implementation guidance.

This plan is intentionally greenfield and explicit about the optimisation behaviour, control semantics, and deterministic validation requirements.

This work is a refactor/replacement of the existing optimiser and sensor behavior already present in `custom_components/energyadvisor/battery_optimizer.py` and `custom_components/energyadvisor/sensor/batterychargemodesensor.py`. The current public API (`BatteryOptimizationInputs`, `BatteryOptimizationResult`, mode strings, and HA attributes) must be preserved unless an explicit compatibility shim is added. The `BatteryChargeModeSensor` entity itself and its Home Assistant wiring, state attributes, update listeners, and entity_id contract must also be preserved; the implementation may replace or refactor the optimization logic behind the sensor, but it must not remove or rename the entity or break the current HA integration connection.

The optimisation output is advisory only. It is intended to surface a battery mode and target SoC for downstream automation or user review; it does not directly control the battery hardware. This document defines the calculation and schedule semantics, not a device-control contract.

The authoritative load forecast source is the existing `HouseholdForecastCoordinator.forecast_slots` output, not a second ad hoc recorder/history cache. The optimiser may use that forecast as its canonical `load_t` series. If that coordinator is unavailable or has insufficient valid history, the implementation may fall back to a legacy historical profile, but this must be treated as a secondary path, not as the main design.

## 1. Scope and non-goals

In scope:
- 48-hour rolling horizon
- 15-minute slots
- HiGHS MILP optimisation
- battery SoC scheduling for the next slot and future horizon
- Home Assistant integration behaviour, state attributes, and fallback handling

Out of scope:
- manual override in Home Assistant
- any legacy charger heuristics as the definitive algorithm
- multi-battery coordination or appliance-level scheduling beyond the already-defined integration inputs

## 2. Canonical configuration inputs

The optimiser must reuse the already-defined integration keys rather than creating new config names. The canonical contract is the set already present in `custom_components/energyadvisor/const.py` and `custom_components/energyadvisor/config_flow_helpers.py`.

Required battery inputs:
- `battery_capacity_kwh`
- `battery_max_charge_power_w`
- `battery_max_discharge_power_w`
- `battery_soc_entity`
- `battery_optimization_enabled`
- `battery_optimization_horizon_hours`
- `battery_min_soc_pct`
- `battery_max_soc_pct`
- `battery_degradation_cost` (optional wear term; default 0 unless configured)

`BatteryOptimizationInputs` must accept an optional `load_forecasts` series, expressed as a chronological list of per-slot kWh values aligned to the optimizer horizon. Each entry must be a normalized 15-minute slot with a UTC/local timestamp converted to the Home Assistant timezone before interpolation. The sensor must subscribe to `runtime_data.household_coordinator.forecast_slots` updates in addition to the existing price, SoC, and solar update triggers. The schedule is still advisory-only; the debug contract is separate from the HA state attributes.

`battery_charge_power_entity` is telemetry only for validation/calibration and must not be treated as a writable control endpoint under the advisory-only contract. It is not required for optimisation logic and should not be used as a runtime device command source.

Optional context inputs already in the integration:
- `grid_import_entity`, `grid_export_entity`
- `power_meter_consumption`
- `outdoor_temperature_entity`
- `water_heater_active_entity`, `water_heater_power_entity`, `water_heater_power_w`, `water_heater_max_hours`
- `central_heating_active_entity`, `central_heating_power_entity`, `central_heating_power_w`
- `bathroom_humidity_entity`
- `pool_pump_power_entity`, `pool_pump_power_w`
- `dehumidifier_power_entity`, `dehumidifier_power_w`

These keys are the authoritative Home Assistant configuration contract for this feature.

## 3. Required battery modes and semantics

The valid modes are exactly:
- `standby`
- `maxuse`
- `charge`
- `discharge`
- `sell`

Definitions:
- `standby`: the battery is idle; no charge or discharge flow is active.
- `maxuse`: the battery is intentionally idle for this slot; no explicit charge or discharge flow is scheduled. This is the default fallback mode and the preferred mode when the optimizer cannot confidently distinguish a profitable action.
- `charge`: the optimizer intends to charge the battery during the slot. The schedule entry must include `target_soc` equal to the end-of-slot SoC target.
- `discharge`: the battery discharges to serve household load. Battery discharge must never charge from PV. It may reduce grid import and may support self-consumption.
- `sell`: the battery discharges for export at a favourable price. The schedule entry must include `target_soc` equal to the end-of-slot SoC target.

Important rules:
- Battery modes are mutually exclusive within a slot: `m_charge_t + m_discharge_t + m_sell_t + m_maxuse_t + m_standby_t = 1`.
- `maxuse` is an idle mode: `b_ch_grid_t = 0`, `b_ch_pv_t = 0`, `b_dis_load_t = 0`, and `b_dis_export_t = 0` whenever `m_maxuse_t = 1`.
- `standby` is also an idle mode: `b_ch_grid_t = 0`, `b_ch_pv_t = 0`, `b_dis_load_t = 0`, and `b_dis_export_t = 0` whenever `m_standby_t = 1`.
- `target_soc` is required for `charge` and `sell` and must be end-of-slot SoC in percent.
- The schedule object must always include a `target_soc` key; for `standby`, `maxuse`, and `discharge`, the value is `null`.
- `maxuse` is preferred when the objective difference is numerically indiscernible or effectively zero.
- `standby` is semantically valid but economically redundant with `maxuse` in this objective; because both are forced-idle modes with the same cost structure, the lexicographic tie-break will normally prefer `maxuse` and the implementation should not require a non-zero `standby` schedule.

## 4. Completing the MILP

### 4.1 Load forecast and PV source

For every future 15-minute slot in the optimization horizon, there must be a scalar `load_t` and a scalar `pv_t` in kWh for that slot.

The implementation contract is:
- `pv_t` is the forecasted solar generation for the slot. It is provided by the integration’s existing solar forecast inputs and normalized into kWh over the actual slot duration; when a price or forecast entry overlaps the active slot boundary, the value is apportioned by overlap duration rather than fixed to a 15-minute block.
- `load_t` is the forecasted household load for the slot. The canonical source is `HouseholdForecastCoordinator.forecast_slots`, which is the integration’s authoritative 48-hour load forecast. It must be used as the model input whenever it is available. If that coordinator is unavailable or lacks sufficient valid history, the implementation may fall back to a historical profile derived from the previous 7 days of the same timeslot pattern, but this must be treated as a secondary path and traced in the entity reason.
- the optimizer must accept one concrete load shape, `load_forecasts`: a list of dicts in the form `{"start": <UTC/local ISO datetime>, "end": <UTC/local ISO datetime>, "load_kwh": float}`. The adapter from `runtime_data.household_coordinator.forecast_slots` must read the actual coordinator keys `slot["from"]` (local slot start, `YYYY-MM-DDTHH:MM`) and `slot["load"]` (kW), derive `end = from + 15 minutes`, and convert `load_kw * actual_slot_duration_hours` to `load_kwh` for each overlapping optimizer slot after converting the timestamp to the Home Assistant local timezone. Fixed `0.25 h` scaling is valid only for a full 15-minute slot; partial first/last slots use the actual overlap duration.
- if the live current load signal is unavailable or invalid, the optimizer must continue with the forecast baseline and mark this in the reason string; it must not silently assume no load.

This requirement is the explicit source of `load_t` for the model and prevents the optimizer from inventing a zero-load horizon.

Availability rule for the household forecast:
- `HouseholdForecastCoordinator.forecast_slots` counts as available whenever it returns a non-empty horizon and the coordinator is not missing. A cold-start 48-hour profile is valid input for the optimizer, but it must be treated as a degraded baseline and the reason string should mention that the forecast is in fallback/cold-start mode.
- the secondary historical-profile path is only used when the coordinator is absent, empty, or explicitly unusable; it is not a separate normal input path whenever the coordinator is present.

### 4.2 Full objective and complete energy-balance equations

For each slot t, define the variables:
- `load_t`: household load in kWh
- `pv_t`: solar generation in kWh
- `g_imp_t`: grid import in kWh
- `g_exp_t`: grid export in kWh
- `b_ch_grid_t`: grid-to-battery charge in kWh
- `b_ch_pv_t`: PV-to-battery charge in kWh
- `b_dis_load_t`: battery discharge to serve household load in kWh
- `b_dis_export_t`: battery discharge to export in kWh
- `pv_to_load_t`: PV used to serve load in kWh
- `pv_to_export_t`: PV directly exported to the grid in kWh
- `soc_t`: battery energy at the end of slot t in kWh
- `headroom_shortfall_t`: non-negative penalty variable for lost PV headroom in kWh
- `d_grid_t` ∈ {0, 1}: mutually exclusive grid direction selector
- `m_charge_t`, `m_discharge_t`, `m_sell_t`, `m_maxuse_t`, `m_standby_t` ∈ {0, 1}

Domain rule:
- all energy-flow and penalty variables are continuous and non-negative: `g_imp_t`, `g_exp_t`, `b_ch_grid_t`, `b_ch_pv_t`, `b_dis_load_t`, `b_dis_export_t`, `pv_to_load_t`, `pv_to_export_t`, `headroom_shortfall_t` >= 0
- mode and direction variables are binary: `m_*_t`, `d_grid_t` ∈ {0,1}
- `soc_t` is continuous and bounded as `soc_min <= soc_t <= soc_max` for all slots, with `soc_0` fixed to the current measured state
- if the measured `soc_0` falls outside `[soc_min, soc_max]`, clamp it to the nearest valid bound before solving and include the clamping in the reason string; do not allow the solver to become infeasible solely because of an out-of-range sensor reading

Complete SoC equation:
- `soc_{t+1} = soc_t + eta_ch * (b_ch_grid_t + b_ch_pv_t) - (b_dis_load_t + b_dis_export_t) / eta_dis`

PV-allocation equation:
- `pv_to_load_t + pv_to_export_t + b_ch_pv_t = pv_t`
- `pv_to_load_t <= load_t`

Complete grid balance:
- `g_imp_t + b_dis_load_t + pv_to_load_t = load_t + b_ch_grid_t`
- `g_exp_t = pv_to_export_t + b_dis_export_t`

These equations are the required provenance check: PV charged into the battery is explicitly allocated from `pv_t`, and grid energy is never relabelled as PV energy.

Objective:
- `minimise sum_t [ price_t * g_imp_t - credit_t * g_exp_t + degradation_cost_t * (b_ch_grid_t + b_ch_pv_t + b_dis_load_t + b_dis_export_t) + reserve_penalty_t ] - terminal_value * soc_T`

where:
- `price_t` is the slot import price, `credit_t` is export credit, and `degradation_cost_t` is the configured degradation cost
- `terminal_value = max(last_import_price, last_export_credit, 0) * eta_dis`
- `reserve_penalty_t` is the solar headroom shortfall penalty defined in section 4.7
- the terminal value is negative in the objective so the solver prefers retaining end-of-horizon energy, rather than penalising it

### 4.3 Battery flow variables and discharge/sell distinction

To keep discharge and sell mathematically distinguishable, the model must use separate variables:
- `b_ch_grid_t`: grid-to-battery charge in kWh
- `b_ch_pv_t`: PV-to-battery charge in kWh
- `b_dis_load_t`: battery discharge that serves household load in kWh
- `b_dis_export_t`: battery discharge dedicated to export in kWh
- `b_dis_t = b_dis_load_t + b_dis_export_t`
- `b_ch_t = b_ch_grid_t + b_ch_pv_t`

Constraints:
- `b_dis_load_t <= load_t`
- `b_dis_export_t <= g_exp_t`
- `b_dis_load_t <= max_discharge_kwh_t * m_discharge_t`
- `b_dis_export_t <= max_discharge_kwh_t * m_sell_t`
- `b_ch_pv_t <= pv_t`
- `b_ch_pv_t <= max_charge_kwh_t * m_charge_t`
- `b_ch_grid_t <= max_charge_kwh_t * m_charge_t`
- `b_ch_grid_t + b_ch_pv_t <= max_charge_kwh_t * m_charge_t`
- `b_ch_pv_t = 0` when `m_discharge_t = 1` or `m_sell_t = 1`
- `b_dis_load_t = 0` when `m_sell_t = 1`
- `b_ch_grid_t = 0`, `b_ch_pv_t = 0`, `b_dis_load_t = 0`, and `b_dis_export_t = 0` whenever `m_maxuse_t = 1`

This makes `maxuse` an idle mode, not a discharge mode with a different label. It preserves a distinct economic `discharge` action while keeping the lexicographic tie-break consistent with explicit load-serving discharge semantics.

### 4.4 Simultaneous import/export prohibition

Grid-direction ambiguity must be eliminated by a binary direction switch:
- `d_grid_t ∈ {0,1}`

Constraints:
- `g_imp_t <= max_import_kwh_t * d_grid_t`
- `g_exp_t <= max_export_kwh_t * (1 - d_grid_t)`

This makes import and export mutually exclusive in the same slot and prohibits physically impossible simultaneous import and export.

### 4.5 Terminal SoC policy

The finite-horizon optimizer must value remaining stored energy at the horizon end.

Required rule:
- add a terminal value term for `soc_T` in the objective as `- terminal_value * soc_T`, where `terminal_value = max(last_import_price, last_export_credit, 0) * eta_dis`
- enforce a terminal reserve floor `soc_T >= min_soc_kwh + reserve_kwh`, where `reserve_kwh` must be bounded by the reachable terminal energy:
  - `reserve_kwh = min(soc_max - soc_min, max(0.10 * capacity_kwh, 0.05 * (soc_max - soc_min)), soc_0 - soc_min + eta_ch * sum_t max_charge_kwh_t)`
- if the calculated reserve is larger than the reachable terminal energy, the model must either drop the hard terminal reserve to the reachable bound or convert the reserve term to a soft penalty using `reserve_shortfall_t` and a high penalty coefficient. The hard form must not be infeasible for short horizons with limited charge power.

The terminal value prevents the optimizer from draining the battery to the minimum immediately before the final slot, while the reserve floor keeps the schedule realistic and prevents pathological horizon-end depletion without making small-horizon solves impossible.

### 4.6 Tie-break / lexicographic solve

The tie-break must be implemented as a deterministic two-pass solve, not as an undefined epsilon test.

Procedure:
1. Solve for the minimum objective value `obj_1`.
2. Add the constraint `objective <= obj_1 + 1e-6`.
3. Then maximise `sum_t m_maxuse_t`.
4. Add the constraint `sum_t m_maxuse_t >= maxuse_2 - 1e-6` where `maxuse_2` is the maximum value from pass 2.
5. If there is still a tie, minimise the total absolute battery flow `sum_t (b_ch_grid_t + b_ch_pv_t + b_dis_load_t + b_dis_export_t)`.

This is the required implementation of “prefer maxuse when it is difficult to make a decision.” It is valid in HiGHS and deterministic.

### 4.7 Grid bounds and remaining reserve logic

For each slot, define the required big-M bounds as:
- `max_import_kwh_t = load_t + b_ch_grid_t_max + b_ch_pv_t_max + 1e-6`
- `max_export_kwh_t = pv_t + b_dis_export_t_max + 1e-6`

where:
- `b_ch_grid_t_max = max_charge_power_w * slot_duration_hours / 1000`
- `b_ch_pv_t_max = max_charge_power_w * slot_duration_hours / 1000`
- `b_dis_export_t_max = max_discharge_power_w * slot_duration_hours / 1000`

This gives a safe upper bound that reflects the actual load, PV, and battery limits without allowing the model to fabricate unbounded grid exchange.

Solar headroom logic:
- define a high-solar threshold `pv_hi = 0.75 * max_t pv_t` over the optimization horizon. If `max_t pv_t <= 0`, set `t_hi = T` and impose no headroom penalty for the full horizon.
- otherwise define the high-solar window as the right-open interval `[t_hi, t_end)` where `t_hi = min { t | pv_t >= pv_hi }` and `t_end` is the first index after the contiguous high-solar run (`t_end = T` if the run reaches the horizon end), such that for all `k` in `[t_hi, t_end)`, `pv_k >= pv_hi`
- apply headroom only for slots before the window start: `t < t_hi`
- define the positive surplus available through the end of the high-solar window as `surplus_pre_hi_t = max(0, sum_{k=t}^{t_end-1} (pv_k - load_k))` for `t < t_hi`, and `headroom_t = min(soc_max - soc_min, surplus_pre_hi_t)`
- define the non-negative shortfall variable `headroom_shortfall_t >= 0` for each `t < t_hi` with the linear constraint `headroom_shortfall_t >= soc_t - (soc_max - headroom_t)`
- add a soft penalty `reserve_penalty_t = penalty_factor * headroom_shortfall_t`, where `penalty_factor = max(last_import_price, last_export_credit, 1.0)`
- this is intentionally a soft headroom policy: the optimizer may trade some headroom against price arbitrage, but it pays a measurable penalty when it consumes empty capacity that could have been reserved for the upcoming PV window

The solar test must validate the penalty behavior, not a hard inequality: it should assert the computed `headroom_t`, `headroom_shortfall_t`, and objective effect for the relevant pre-window slots, rather than requiring `soc_t <= soc_max - headroom_t` as a hard feasibility rule.

The terminal reserve is capped to avoid infeasibility on narrow usable SoC ranges and short horizons:
- `reserve_kwh = min(soc_max - soc_min, max(0.10 * capacity_kwh, 0.05 * (soc_max - soc_min)), soc_0 - soc_min + eta_ch * sum_t max_charge_kwh_t)`
- the implementation must never enforce a terminal reserve larger than the available usable SoC range or the energy that can be reached by the end of the horizon

### 4.8 Horizon policy

The authoritative configuration value remains `battery_optimization_horizon_hours`.

Default and supported range:
- default value: `48.0`
- allowed range: `1.0` to `48.0`
- the optimizer must clip the final schedule to the configured horizon value and never exceed 48 hours in the HA integration

## 5. Schedule and attribute contract

The runtime output must be a list of per-slot schedule entries in chronological order.

The schedule is anchored to the start of the current local day and always starts at `00:00` for that date. It includes the current in-progress slot and all future slots for the configured horizon; past slots are historical and must not be rewritten or re-optimised on subsequent quarter-hour updates.

Each entry shape:
- `from`: local wall-clock time, `YYYY-MM-DDTHH:MM`
- `mode`: one of `standby`, `maxuse`, `charge`, `discharge`, `sell`
- `target_soc`: always included, with `null` for `standby`, `maxuse`, and `discharge`, and a numeric end-of-slot SoC percentage for `charge` and `sell`
- `cost`: import cost for the slot if available
- `credit`: export credit for the slot if available

Rules:
- `target_soc` must be the end-of-slot SoC percentage for `charge` and `sell`
- `target_soc` is always present in the JSON object, but `null` is allowed for the non-target modes
- schedule entries must preserve the native 15-minute resolution and the current time offset
- the current active mode is resolved from the entry whose slot contains the current wall-clock time
- the optimizer recomputes at a 15-minute cadence, but only the current and future slots are updated; entries earlier than the current local time remain fixed as historical schedule records

Example schedule payload:

  [
    {"from": "2026-09-13T07:00", "mode": "charge", "target_soc": 72.0},
    {"from": "2026-09-13T07:15", "mode": "maxuse", "target_soc": null}
  ]

## 6. Fallback and failure policy

### 6.1 Canonical fallback mode

The fallback mode is `maxuse`.

No manual override feature exists in this integration. The fallback path is therefore strictly algorithmic and must never depend on persistent user control.

### 6.2 Failure categories

The optimiser must classify failures into explicit categories and emit a corresponding human-readable reason string.

Required categories:
- configuration invalid
- SoC unavailable
- price data unavailable
- solver failure
- unsupported/invalid entities
- empty or malformed horizon

Required policy:
- invalid configuration values reject the optimisation and fall back to `maxuse`
- missing SoC falls back to `maxuse`
- missing or malformed price data falls back to `maxuse`
- solver failure falls back to `maxuse`
- a single malformed rate or timestamp entry may be discarded only if the remaining data still yields a valid optimization set; otherwise the optimizer must fall back

The reason string must be explicit and stable, e.g.:
- "Battery SoC sensor is unavailable; using maxuse fallback."
- "Battery SoC bounds are invalid; using maxuse fallback."
- "HiGHS failed to solve the schedule; using maxuse fallback."

### 6.3 HA state attributes

The sensor must expose:
- `state`: current mode
- `modes`: list of available modes and metadata
- `reason`: fallback or optimization reason
- `optimized`: `true` or `false`
- `solver`: solver name or `null`
- `current_soc_pct`: current SoC percentage if available
- `current_target_soc`: current slot `target_soc` if present

For test/debug validation, the implementation may expose an internal `SolverDebugResult` or `FlowResult` structure containing per-slot `g_imp_t`, `g_exp_t`, `b_dis_load_t`, `b_dis_export_t`, and `soc_t` values. This structure must not be required by the public HA state API and should remain out of the normal entity attributes unless a debug flag is explicitly enabled.

## 7. Home Assistant control semantics

The optimiser does not directly call a charger or write a control command to the battery. It emits a computed battery mode schedule and exposes the active mode as a sensor state.

Required behaviour:
- the state is derived from the schedule at the current wall-clock time
- no separate manual override path is part of this integration
- updates must be triggered by battery SoC updates, price sensor updates, and forecast updates in Home Assistant
- the entity must remain valid even if the optimiser cannot run; it must show `maxuse` fallback and a reason string

## 8. Implementation steps

### Step 1: Freeze the optimisation contract and validate the config keys

Required work:
- confirm the optimizer uses the canonical config keys already in `const.py` and `config_flow_helpers.py`
- validate that the runtime inputs match the required battery and context contract
- define the default fallback path as `maxuse`

Home Assistant validation:
- install the integration in a dev installation and confirm the options flow still exposes the existing battery and optimization inputs
- ensure `sensor.energy_advisor_battery_charge_mode` can be created without inventing new config names

### Step 2: Build the normalized time-slot model

Required work:
- convert all price entries into 15-minute normalized slots for the active horizon
- clip to the current time and the 48-hour horizon
- preserve partial current-slot duration correctly
- when source price/credit records cover a longer interval than 15 minutes, keep the value constant across the original interval and split it across local quarter-hour boundaries; partial first slots are allowed at the active start time
- reject or discard invalid timestamps and values according to the failure policy
- for overlapping or conflicting rate entries, keep the record with the greatest start timestamp; if two records share the same start timestamp, use a deterministic precedence order (source priority, then `last_updated` if available, then insertion order as the final tie-break), and reject ambiguous overlaps if no precedence is defined

Home Assistant validation:
- confirm the entity output schedule covers the expected 48-hour window with one entry per 15-minute period
- verify the current slot is anchored to current local time in HA

### Step 3: Implement the HiGHS MILP

Required work:
- add binary mode variables for `charge`, `discharge`, `sell`, `maxuse`, and `standby`
- implement the mutually exclusive constraint
- add the SoC balance, grid balance, bound constraints and objective terms
- include optional degradation cost if configured
- set tie-breaking to prefer `maxuse` under near-indifference

Home Assistant validation:
- in the dev installation, confirm that the optimizer resolves charging before expensive evening windows and discharge/sell during expensive export windows when the economics justify it

### Step 4: Implement the `target_soc` schedule and mode mapping

Required work:
- include the `target_soc` key on every schedule entry, with numeric end-of-slot SoC for `charge` and `sell` and `null` for `maxuse`, `standby`, and `discharge`
- calculate end-of-slot SoC percentage
- clamp target values to the configured min/max SoC bounds

Home Assistant validation:
- confirm the mode and target values match the live schedule at the current time in the UI

### Step 5: Add robustness and fallback logic

Required work:
- if config is invalid, SoC is missing, price slots are empty, or the solver fails, return the `maxuse` fallback schedule
- emit explicit reason strings
- keep the entity valid and observable even when optimization is unavailable
- ensure `optimized` is `false` in the fallback case and `true` on valid solved schedules

Home Assistant validation:
- in the dev installation, turn off optimization or corrupt the SoC configuration and confirm `state` remains valid, `optimized` is `false`, and `reason` explains the fallback

### Step 6: Wire the optimizer to HA runtime updates

Required work:
- subscribe to price updates at the existing 15-minute cadence
- subscribe to SoC updates
- recompute on household-forecast updates via `runtime_data.household_coordinator.forecast_slots`
- recompute on solar/forecast updates
- re-anchor the schedule to the start of the current local day and preserve all historical slots before the current time as fixed records
- expose state attributes for debugability

Home Assistant validation:
- confirm the mode changes when SoC changes, household load forecast changes, or when the live electricity price feed changes without a restart
- confirm the array starts at the start of the current day and that only the current and future slots change while past entries remain unchanged

### Step 7: Keep the code path deterministic and testable

Required work:
- use explicit fixture-based tests for price spreads, solar reserve behavior, invalid SoC bounds, and solver failures
- add assertions for objective direction, target SoC bounds, and mode exclusivity
- keep the Home Assistant dev installation as a live manual verification layer, not as the only test path

Home Assistant validation:
- run the pytest battery optimizer tests and then confirm the same logic holds in the live dev installation with real entity data

### Step 8: End-to-end verification in the dev installation

Required work:
- verify the entity state, schedule, and attributes against live price/solar/battery data over a full day cycle
- confirm that a stale or unavailable SoC produces the correct fallback reason and `maxuse` state
- validate that schedule transitions occur naturally with the current HA data stream

Home Assistant validation:
- check the `reason`, `optimized`, `solver`, `current_soc_pct`, `current_target_soc`, and current `mode` in the HA developer tools state panel

### Step 9: Release gates

Required work:
- all deterministic tests pass
- all critical fallback scenarios are covered
- the battery mode sensor remains valid under invalid or missing data
- no manual override feature is exposed
- no part of the implementation depends on `docs/old`

Home Assistant validation:
- complete a live integration check in the dev HA environment with the actual installation config and the expected day-ahead price/solar profile

## 9. Deterministic test matrix

The following cases are mandatory before sign-off. Each case must include exact numeric fixtures and expected invariants. Every solver fixture must include at least: `slot_duration_hours`, `eta_ch`, `eta_dis`, `capacity_kwh`, `max_charge_power_w`, `max_discharge_power_w`, `min_soc_pct`, `max_soc_pct`, `soc_0_kwh`, `price_t`, `credit_t`, `pv_t`, `load_t`, and the terminal-reserve/scenario inputs required by the objective. Unless a case explicitly overrides a value, all tests use the shared baseline fixture: `slot_duration_hours = 0.25`, `eta_ch = 0.95`, `eta_dis = 0.95`, `max_charge_power_w = 5000`, `max_discharge_power_w = 5000`, `degradation_cost = 0.0`, `pv_t = [0.0, 0.0, 0.0, 0.0]`, and `load_t = [0.0, 0.0, 0.0, 0.0]` unless the scenario specifies otherwise. All omitted price/load/PV inputs inherit from this baseline.

1. Cheap charge then expensive sell:
   - capacity = 10 kWh, SoC0 = 5 kWh, min = 2 kWh, max = 8 kWh
   - max charge = 5000 W, max discharge = 5000 W, eta_ch = 0.95, eta_dis = 0.95, slot_duration = 0.25 h
   - prices = [0.10, 0.10, 0.80, 0.80] SEK/kWh, credits = [0.05, 0.05, 0.90, 0.90]
   - PV = [0.0, 0.0, 0.0, 0.0], load = [0.5, 0.5, 0.5, 0.5]
   - expected first slot mode = `charge`, expected first target_soc within [20%, 80%], and all constraints must keep SoC within bounds

2. Expensive discharge:
   - capacity = 10 kWh, SoC0 = 7 kWh, min = 2 kWh, max = 8 kWh
   - max charge = 5000 W, max discharge = 5000 W, eta_ch = 0.95, eta_dis = 0.95, slot_duration = 0.25 h
   - prices = [0.70, 0.70, 0.10, 0.10], credits = [0.05, 0.05, 0.05, 0.05]
   - PV = [0.0, 0.0, 0.0, 0.0], load = [1.0, 1.0, 1.0, 1.0]
   - expected first slot mode = `discharge`
   - require `b_dis_load > 0`, `b_dis_export = 0`, and `g_imp_t * g_exp_t == 0` in every solved slot

3. Flat price / no trade:
   - capacity = 10 kWh, SoC0 = 5 kWh, min = 2 kWh, max = 8 kWh
   - max charge = 5000 W, max discharge = 5000 W, eta_ch = 0.95, eta_dis = 0.95, slot_duration = 0.25 h
   - prices = [0.50, 0.50], credits = [0.50, 0.50]
   - PV = [0.0, 0.0], load = [0.0, 0.0]
   - expected mode = `maxuse` and `optimized = true` under the lexicographic tie-break

4. Solar reserve:
   - capacity = 10 kWh, SoC0 = 4 kWh, min = 2 kWh, max = 8 kWh
   - max charge = 5000 W, max discharge = 5000 W, eta_ch = 0.95, eta_dis = 0.95, slot_duration = 0.25 h
   - PV forecast = [0.0, 0.0, 2.0, 2.0], prices = [0.10, 0.60, 0.60, 0.60], credits = [0.05, 0.05, 0.05, 0.05]
   - load = [0.3, 0.3, 0.3, 0.3]
   - expected optimizer to preserve headroom before the PV window without violating terminal reserve constraints
   - assert `pv_hi = 1.5`, `t_hi = 2`, and `headroom_t = max(0, sum_{k=t}^{t_end-1}(pv_k - load_k))` for each pre-window slot `t < t_hi`; require `headroom_shortfall_t >= soc_t - (soc_max - headroom_t)` and the objective includes the soft penalty `penalty_factor * headroom_shortfall_t`

5. Invalid SoC bounds:
   - min_soc_pct = 80, max_soc_pct = 60
   - expected fallback to `maxuse`, `optimized = false`, and reason string starts with “Battery SoC bounds are invalid”

6. Missing SoC:
   - SoC entity unavailable or invalid
   - expected fallback to `maxuse`, `optimized = false`, and reason string includes “unavailable”

7. Solver failure:
   - malformed or inconsistent data causes HiGHS to fail
   - expected fallback to `maxuse`, `optimized = false`, and reason string includes “HiGHS failed”

8. Quarter-hour fidelity:
   - four 15-minute slots in the horizon
   - price = [0.20, 0.20, 0.20, 0.20], credit = [0.05, 0.05, 0.05, 0.05], PV = [0.0, 0.0, 0.0, 0.0], load = [0.0, 0.0, 0.0, 0.0]
   - schedule length must be 4 and timestamps must retain local 15-minute boundaries

9. No simultaneous import/export:
   - use a fixture that forces import in one slot and export in another without any overlap. A valid example is: capacity = 10 kWh, SoC0 = 7 kWh, min = 2 kWh, max = 8 kWh, slot_duration = 0.25 h, prices = [0.10, 0.90, 0.10, 0.90], credits = [0.05, 1.00, 0.05, 0.05], PV = [0.0, 0.0, 2.0, 0.0], load = [0.2, 0.5, 0.5, 0.5]. This provides a genuine export opportunity in a separate slot without relying on a zero-PV/low-credit fixture that cannot force export. Assert `g_imp_t * g_exp_t == 0` for each solved slot while the model still chooses a valid import slot and a valid export slot in different slots.

10. Terminal reserve:
   - set SoC0 high and price end low, with capacity = 10 kWh, SoC0 = 7 kWh, min = 2 kWh, max = 8 kWh, PV = [0.0, 0.0, 0.0, 0.0], load = [0.2, 0.2, 0.2, 0.2], prices = [0.10, 0.10, 0.10, 0.10], credits = [0.05, 0.05, 0.05, 0.05], and ensure `soc_T >= min_soc_kwh + reserve_kwh` in the final solution

## 10. Acceptance checklist

The implementation is complete only when all of the following are true:

- the optimizer is implemented as a HiGHS MILP with mutually exclusive mode variables
- the canonical SoC equation and grid balance described above are implemented precisely
- `charge` and `sell` include `target_soc` as end-of-slot SoC percentage
- `maxuse` is the fallback mode and the preferred tie-break default
- `optimized` is `false` when the optimizer falls back and `true` when the solver succeeds
- the battery never charges from PV while in discharge mode
- the schedule is derived from the same flow equations used for objective and state constraints
- invalid data and solver failures degrade to `maxuse` with an explicit reason string
- no manual override has been introduced
- all deterministic tests pass and the dev Home Assistant installation validates the same logic with real entities

## 11. Implementation note

This plan intentionally does not rely on `docs/old`; those files are deprecated and must not be used as implementation guidance.
