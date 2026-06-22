# Battery and Flexible-Load Optimizer README

This file is the working plan and progress tracker for expanding the current
price-only battery scheduler into a profit-first planner that also considers
solar production, household demand, and flexible loads.

Update this file as work lands so progress is visible and the plan survives
agent or computer restarts.

## Input sensors needed

### Required for the target solution

- [x] `nordpool_prices_sensor` — existing price input.
- [x] `forecast_entity` — existing solar forecast input for today.
- [x] `forecast_tomorrow_entity` — existing solar forecast input for tomorrow.
- [x] `power_entity` — existing inverter power input for the refined solar forecast.
- [ ] `battery_soc_entity` — required to know how much usable energy is currently stored.
- [ ] `bathroom_humidity_entity` — when humidity reaches `100%`, treat it as a shower and reset the water-heater 24-hour timer.
- [ ] `outdoor_temperature_entity` or temperature forecast entity — required to predict heating-driven winter load.
- [ ] `water_heater_power_entity` and `water_heater_power_w` — required to measure actual reheating energy after a hot-water event and verify heater recovery.
- [ ] `pool_pump_power_entity` and `pool_pump_power_w` — required to schedule the pool pump only in sunny or surplus slots.
- [ ] `dehumidifier_power_entity` and `dehumidifier_power_w` — required to schedule the dehumidifier only in sunny or surplus slots.

> **Naming note:** canonical config keys use `*_power_entity` for appliance power
> sensors: `water_heater_power_entity`, `pool_pump_power_entity`, and
> `dehumidifier_power_entity`.

### Recommended for better verification and learning

- [ ] `battery_charge_power_entity` — helps verify real charge/discharge behavior and improve the planner.
- [ ] Grid power input (`grid_import_entity`, `grid_export_entity`, or a net grid power entity) — helps verify that the chosen mode really reduced cost or increased profit.
- [ ] `household_base_load_w` — useful as an initial fallback before a learned load model is good enough.

## Target one-word battery modes

- `standby` — preserve battery energy for later; no deliberate charge or discharge.
- `charge` — charge from the grid because the future value of stored energy is higher.
- `maxuse` — maximize self-consumption; store solar surplus and later use the battery for house load.
- `discharge` — use battery energy for house load now when that beats importing now.
- `sell` — export battery energy because selling now is worth more than keeping it.

## Water-heater rule

- `4` hours is the **worst-case upper bound**, not the fixed daily target.
- A bathroom humidity reading of `100%` is treated as a shower event and resets
  the current hot-water cycle.
- `water_heater_power_entity` cannot directly reveal shower length in advance.
- Instead, heater power can be integrated **after** the event to measure how
  much reheating actually happened and when the tank recovered.
- The first planner should therefore reset to a conservative refill need on the
  shower trigger, then reduce or close that need as actual heater recovery is
  observed, while keeping the 4-hour equivalent as a safety cap.

## Scope of the first implementation

- The integration should stay **advisory first**: it publishes recommendations,
  reasons, and schedules before it is trusted to drive automations.
- Device actuation can remain in Home Assistant automations while the integration
  computes the plan.
- Learning should improve forecasts and reserve estimates, but mode selection
  should stay deterministic and explainable.

## Current repo state

- Implemented today:
  - `sensor.electricitypricelevels`
  - `sensor.batterychargemode` with price-only `standby` / `charge` / `discharge`
  - optional `sensor.solarforecast`
- Wired into config storage but not yet used by runtime planning:
  - `battery_soc_entity`
  - `household_base_load_w`
  - `pool_pump_power_*`
  - `water_heater_power_*`
  - `bathroom_humidity_entity`
  - `dehumidifier_power_*`
  - `outdoor_temperature_entity`
  - `grid_import_entity`
  - `grid_export_entity`
  - `battery_charge_power_entity`
- Not yet used by runtime planning:
  - the stored optimizer inputs above

## Verifiable baby steps

Where a live rollout is useful, the step includes an explicit **Deploy**
substep. Here, **Deploy** means releasing to the live Home Assistant system.

1. [x] Create this roadmap file.
   - **Done when:** this file exists in `docs/` and becomes the source of truth for progress notes.

2. [x] Lock current battery behavior with tests.
   - **Deliverable:** add or tighten tests that pin the current price-only battery logic.
   - **Verify:** existing battery behavior remains unchanged before new features begin.
   - **Note:** `test_battery_charge_mode_sensor.py` already covers the core algorithm well.
     Added edge-case coverage for flat price curves (no charge/discharge expected), only
     today's prices available (no tomorrow data), and a spring DST transition day.

3. [x] Add config plumbing for the new inputs.
   - **Deliverable:** add constants, config-flow fields, option-flow fields, and translations for the new entities and parameters.
   - **Verify:** the new inputs can be configured from the UI and survive reloads.
   - **Naming outcome:** canonical appliance sensor keys now use `*_power_entity` (`water_heater_power_entity`, `pool_pump_power_entity`, `dehumidifier_power_entity`).
   - **Mode naming note:** add `maxuse` and `sell` to the `batterychargemode` state translations in `strings.json` and both translation files no later than step 15. If localized shadow-mode output is wanted earlier, expose `proposed_mode` as a dedicated diagnostic sensor instead of a plain attribute.
   - **Done:** added config/options storage, validation, and translated labels for the agreed optimizer inputs; documented that they are stored now but not yet used by the runtime planner.
   - **Deploy:** not by itself; bundle this with step 4 and step 5 for the first live release.

4. [x] Make the battery sensor explain itself.
   - **Deliverable:** add attributes such as `reason`, `next_mode_change`, `reserved_kwh`, `required_load_kwh`, and `charge_source`.
   - **Verify:** every mode decision can be understood from sensor attributes without reading logs.
   - **Note:** design these attributes to describe the *chosen mode* rather than internals of the
     current price-only algorithm, so they remain meaningful when the new mode is promoted in
     step 15 without requiring a rewrite.
   - **Done:** the battery helper now exposes human-readable `reason`, block-based `next_mode_change`,
     zeroed `reserved_kwh` / `required_load_kwh` placeholders for future reserve/load logic, and
     `charge_source` for current charging periods.
   - **Deploy:** prepare for live release, but release together with step 5 as **R1**.

5. [ ] Add real battery constraints.
   - **Deliverable:** use SoC, reserve floor, and efficiency to block impossible actions.
   - **Verify:** low SoC cannot produce `discharge` or `sell`; full battery cannot produce `charge`.
   - **Deploy:** **Yes — R1.** Release step 3 + step 4 + step 5 together to the live system. Then observe that the new attributes are useful and that SoC only blocks impossible states.

6. [ ] Introduce the new mode set in shadow mode.
   - **Deliverable:** compute `standby`, `charge`, `maxuse`, `discharge`, and `sell` as a `proposed_mode` attribute while the main state still uses the current logic.
   - **Verify:** old and proposed behavior can be compared safely for a few days.
   - **Note:** after releasing to the live system, let it soak for at least several days on real
     price and solar data before continuing to step 7. The shadow period is the primary
     quality gate before promotion.
   - **Deploy:** **Yes — R2.** Release this step to the live system and start the first shadow-mode soak period.

7. [ ] Replace fixed battery duration with required-energy math.
   - **Deliverable:** stop assuming a fixed discharge length and instead compute required energy until the next useful solar window.
   - **Verify:** the same price curve yields different reserve decisions in summer-like and winter-like scenarios.
   - **Deploy:** not yet; keep in development until step 12 so the advisory planner can be released as a coherent whole.

8. [ ] Add a simple household load model.
   - **Deliverable:** start with `household_base_load_w`, then allow temperature to adjust the expected load upward in winter.
   - **Verify:** winter test scenarios reserve more battery energy than summer scenarios.
   - **Deploy:** not yet; hold for the step 12 advisory-planner release.

9. [ ] Add an advisory water-heater planner.
   - **Deliverable:** compute the cheapest feasible water-heater refill plan, using 4 hours only as the worst-case cap and preferring solar-surplus slots when practical.
   - **Verify:** worst-case scenarios still receive the equivalent of 4 heater hours, while lighter-demand scenarios can schedule less.
   - **Deploy:** not yet; hold for the step 12 advisory-planner release.

10. [ ] Add the shower reset rule for the water heater.
   - **Deliverable:** when bathroom humidity reaches `100%`, reset the current hot-water cycle; then use the water-heater power trace only to measure actual recovery after the event and to detect when reheating is complete.
   - **Verify:** after a simulated shower trigger, the planner starts from the conservative need and then shortens or closes the refill requirement if observed heater recovery ends early.
   - **Deploy:** not yet; hold for the step 12 advisory-planner release.

11. [ ] Add advisory sunny-only planners for the pool pump and dehumidifier.
   - **Deliverable:** schedule both loads only in slots with sufficient solar surplus or clearly positive solar economics.
   - **Verify:** their schedules remain empty in non-sunny test scenarios.
   - **Deploy:** not yet; hold for the step 12 advisory-planner release.

12. [ ] Make the battery planner reserve energy for planned loads.
   - **Deliverable:** the battery planner must consider water-heater demand, expected base load, and sunny-only loads before allowing `discharge` or `sell`.
   - **Verify:** the planner does not sell battery energy that is needed later the same night or before the next solar window.
   - **Deploy:** **Yes — R3.** Release steps 7–12 together so the full advisory planner runs live in shadow mode, then start the second soak period.

13. [ ] Add historical learning.
   - **Deliverable:** learn expected base load from hour, weekday/weekend, season, and temperature; learn solar-surplus confidence from forecast versus actual production. `battery_charge_power_entity` feeds the charge/discharge learning here.
   - **Verify:** the learned model outperforms the fixed-load fallback on held-out historical days.
   - **Data strategy:** use `HA_URL` and `HA_TOKEN` (already in `dev_config.py`) to fetch sensor
     history from the live instance and store the result as test fixtures. Tests must be runnable
     offline without a live HA connection. Sanitize the fixtures before committing them so no
     secrets or unnecessarily detailed household traces end up in the repository. This is a
     **dev/offline step** — no live release needed.
   - **Deploy:** no — development/offline only.

14. [ ] Add backtesting and scenario comparison.
   - **Deliverable:** replay known days and compare old planner versus new planner on cost/profit and rule compliance.
   - **Verify:** lower net cost or higher net profit without breaking hard constraints.
   - **Note:** uses the same pre-fetched fixtures from step 13. Dev/offline only — no live release
     needed. Completion of this step is the confidence gate before step 15.
   - **Deploy:** no — development/offline only.

15. [ ] Promote the proposed mode to the main sensor state.
   - **Deliverable:** once shadow mode and backtests are good enough, publish the new mode set as the real battery state. Update `docs/batterychargemode.md` to document the new mode set and decision logic.
   - **Verify:** scenario tests, regression tests, and documentation all match the new behavior.
   - **Deploy:** **Yes — R4.** Release this only after the shadow-mode soak periods and offline backtesting both look safe.

## Release gates

Most steps are advisory additions or dev/offline work. The first live behavior change can happen
in step 5 when SoC constraints block impossible states, while step 15 is the point where the main
battery mode sensor changes to the new mode set. Some steps add enough observable value that they
should be released to the live system so real data can validate the plan:

| Release | After step | What you gain |
|---------|-----------|---------------|
| **R1** | 4 (+ 5) | Battery decisions are explainable from sensor attributes; SoC constraints make modes more realistic. Low risk, but live behavior can tighten if SoC blocks an impossible action. |
| **R2** | 6 | Shadow mode is live. Start the real-world comparison period. Let it soak for **at least several days** on real price and solar data before continuing. This is the primary quality gate. |
| **R3** | 12 | Full advisory planner is live in shadow mode (battery + water heater + pool + dehumidifier). Start a second soak period before promoting. |
| **R4** | 15 | The release that changes the main battery sensor to the new mode set. Should feel safe because shadow mode has been running for weeks. |

Steps 13–14 are **dev/offline only** and do not require a live release.

## Likely files to touch

- `custom_components/electricitypricelevels/const.py`
- `custom_components/electricitypricelevels/config_flow.py`
- `custom_components/electricitypricelevels/sensor/batterychargemodesensor.py`
- `custom_components/electricitypricelevels/sensor/__init__.py`
- `custom_components/electricitypricelevels/strings.json`
- `custom_components/electricitypricelevels/translations/en.json`
- `custom_components/electricitypricelevels/translations/sv.json`
- `docs/batterychargemode.md` *(update at step 15)*
- `README.md`
- `tests/test_battery_charge_mode_sensor.py`
- New helper modules for optimization logic (see implementation note below)

## Implementation note

The cleanest structure is likely to keep `sensor.batterychargemodesensor.py`
small and move the new optimization logic into dedicated helpers instead of
continuing to grow one file around the current price-only algorithm.
