# Battery Charge Mode Sensor

## Purpose

Determines whether a home battery should be **charging**, **discharging**, or in **standby** based on the electricity price schedule from the linked price sensor in the same config entry. The goal is to charge during cheap slots and discharge during expensive slots within each 12-hour window, while requiring a minimum price spread to prevent unnecessary cycling that would accelerate battery degradation.

---

## Input

### Required

The linked Electricity Price Levels sensor for the same config entry must be available. The battery sensor reads the price sensor's compact rate payload directly and follows the same per-entry wiring as the compact and solar sensors.

### Configuration (all optional — defaults are reasonable without a battery configured)

Set during the final **battery** step of the initial setup flow or later via **Settings → Devices & Services → Electricity Price Levels → Configure**:

| Option | Key | Default | Description |
|---|---|---|---|
| Battery capacity | `battery_capacity_kwh` | — | kWh. Used to compute charging time. |
| Max charge power | `battery_max_charge_power_w` | — | W. Used to compute charging time. |
| Degradation cost margin | `battery_degradation_cost` | 0.7 | Minimum cost-unit spread between cheapest and most expensive slot before a charge/discharge cycle is scheduled. Prevents cycling on near-flat price days. |

When capacity and power are both set:
- `charging_time_minutes = capacity_kwh / (max_power_w / 1000) × 60`
- `discharging_time_minutes = charging_time_minutes × 1.5`

Otherwise falls back to 160 min charge / 240 min discharge.

The config flow also stores additional planner inputs such as `battery_soc_entity`,
`battery_charge_power_entity`, `grid_import_entity`, `grid_export_entity`,
`outdoor_temperature_entity`, `household_base_load_w`,
`water_heater_power_entity`, `water_heater_power_w`, `water_heater_max_hours`,
`bathroom_humidity_entity`, `pool_pump_power_entity`, `pool_pump_power_w`,
`dehumidifier_power_entity`, and `dehumidifier_power_w`. Those fields are for
the staged optimizer rollout and are not used by the current price-only battery
mode algorithm yet.

---

## Output sensor

**Default entity ID:** `sensor.electricity_price_levels_battery_charge_mode` for the first config entry.
Additional entries receive the usual Home Assistant numeric suffixes, such as
`sensor.electricity_price_levels_battery_charge_mode_2`.

### State

One of: `charge` | `discharge` | `standby`

Icon changes dynamically: `mdi:battery-charging` / `mdi:battery-arrow-down-outline` / `mdi:battery-outline`.

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `charge_entries` | list[dict] | Full schedule — one dict per price slot with local `from`, `mode`, and `cost` (`YYYY-MM-DDTHH:MM`) |
| `margin` | float | Configured or default degradation cost margin |
| `charging_time_minutes` | int | Computed or default charging duration |
| `discharging_time_minutes` | int | Computed or default discharging duration |
| `reason` | str | Human-readable explanation for the currently chosen mode |
| `next_mode_change` | str \| null | Local time string (`YYYY-MM-DDTHH:MM`) for the next expected mode change |
| `reserved_kwh` | float | Battery energy currently reserved for future needs; `0.0` until step 5/6 adds reserve logic |
| `required_load_kwh` | float | Load-backed energy target for the current plan; `0.0` until later load-aware steps |
| `charge_source` | str \| null | `grid` while the current mode is `charge`, otherwise `null` |

When `exclude_from_recording` is enabled for the integration, the whole sensor
is excluded from recorder/history. Even when recorder is enabled for the
sensor, the large `charge_entries` attribute is excluded from recorder
attribute storage.

---

## Algorithm

The schedule is recomputed whenever the linked price sensor changes (i.e. when new Nordpool prices arrive). It processes the price list in **12-hour windows** sliding from midnight:

1. **Find discharge peaks** (`_find_local_peaks`): locate the most expensive slots in each window. The global price maximum defines the peak; slots around it are widened to fill `discharging_time_minutes` of total discharge. If the peak–valley spread is below `margin`, the window is skipped entirely.
2. **Find charge valleys** (`_find_local_valleys`): for each discharge block, look back up to 8 hours and pick the cheapest slots that cover `charging_time_minutes`.
3. **Extend peaks** (`_extend_peaks`): if at least one explicit charge/discharge slot was found, extend discharge before the first scheduled event, after the last, and across standby gaps between a discharge block and the next charge block.

If the margin guard rejects all cycles for the day, the schedule remains
`standby` throughout instead of forcing discharge on flat-price days.

A background task (`_periodic_update`) wakes at each slot boundary to advance the current mode and re-evaluate. The linked price sensor notifies the battery sensor when new rate data arrives, so the schedule is recomputed without relying on fixed entity IDs.

Rate changes are detected via a hash of `(from, cost)` tuples to avoid redundant recomputation.

---

## Architecture

```
ElectricityPriceLevelsSensor (same config entry)
    │
    └─► BatteryChargeModeSensor._handle_source_update()
              │
              ├── rates hash changed?  →  compute_charge_modes()  →  _charge_entries
              └── _update_current_mode()  →  self._mode
                        │
                        └─► async_write_ha_state()

Background task: _periodic_update()
    Sleeps until slot boundary, then calls _refresh_from_source()
```

**Key files:**
- `sensor/batterychargemodesensor.py` — all logic: algorithm functions + battery sensor entity
- `const.py` — `CONF_BATTERY_CAPACITY_KWH`, `CONF_BATTERY_MAX_CHARGE_POWER_W`, `CONF_BATTERY_DEGRADATION_COST`
- `tests/test_battery_charge_mode_sensor.py` — ~600 lines, covers algorithm edge cases, config defaults, state transitions

---

## Notes

- Battery timing overrides are optional. If `battery_capacity_kwh` and `battery_max_charge_power_w` are both left empty, the integration falls back to the default 160-minute charge and 240-minute discharge timings.
- `battery_capacity_kwh` and `battery_max_charge_power_w` must be provided together when overriding the defaults.
