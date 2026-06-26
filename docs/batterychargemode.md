# Battery Charge Mode Sensor

> **Scope note:** this document describes the **Energy Advisor** battery helper.

## Purpose

Determines whether a home battery should be in **maxuse**, **sell**, or
occasionally **standby** based on the electricity price schedule from the
linked price sensor in the same config entry. The current temporary summer
strategy keeps the battery in `maxuse` by default and marks only the highest
valued morning/evening slots as `sell`.

---

## Input

### Required

The linked price sensor for the same config entry must be available. The battery sensor reads the price sensor's compact rate payload directly and follows the same per-entry wiring as the compact and solar sensors.

### Configuration (all optional — defaults are reasonable without a battery configured)

Set during the final **battery** step of the initial setup flow or later via **Settings → Devices & Services → Energy Advisor → Configure**:

| Option | Key | Default | Description |
|---|---|---|---|
| Battery capacity | `battery_capacity_kwh` | — | kWh. Retained for future battery planner work. |
| Max charge power | `battery_max_charge_power_w` | — | W. Retained for future battery planner work. |
| Degradation cost margin | `battery_degradation_cost` | 0.7 | Retained planner setting for future battery logic. |
| Battery SoC sensor | `battery_soc_entity` | — | Retained for future battery planner work. |

When capacity and power are both set:
- `charging_time_minutes = capacity_kwh / (max_power_w / 1000) × 60`
- `discharging_time_minutes = charging_time_minutes × 1.5`

Otherwise falls back to 160 min charge / 240 min discharge.

The config flow also stores additional planner inputs such as
`battery_charge_power_entity`, `grid_import_entity`, `grid_export_entity`,
`outdoor_temperature_entity`, `household_base_load_w`,
`water_heater_power_entity`, `water_heater_power_w`, `water_heater_max_hours`,
`bathroom_humidity_entity`, `pool_pump_power_entity`, `pool_pump_power_w`,
`dehumidifier_power_entity`, and `dehumidifier_power_w`. Those fields are for
the staged optimizer rollout and are not used by the current summer battery
mode algorithm yet.

---

## Output sensor

**Default entity ID:** `sensor.energy_advisor_battery_charge_mode` for the first config entry.
Additional entries receive the usual Home Assistant numeric suffixes, such as
`sensor.energy_advisor_battery_charge_mode_2`.

### State

Scheduled slots use `maxuse` or `sell`. `standby` is still used when no price
data is available or the current time is outside the available horizon.

Icon changes dynamically: `mdi:home-lightning-bolt-outline` for `maxuse`,
`mdi:battery-arrow-up-outline` for `sell`, and `mdi:battery-outline` for
`standby`.

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `charge_entries` | list[dict] | Full schedule — one dict per price slot with local `from`, `mode`, and `cost` (`YYYY-MM-DDTHH:MM`) |
| `margin` | float | Retained planner setting for future battery logic |
| `charging_time_minutes` | int | Retained battery timing value for future battery logic |
| `discharging_time_minutes` | int | Retained battery timing value for future battery logic |
| `reason` | str | Human-readable explanation for the currently chosen mode |
| `next_mode_change` | str \| null | Local time string (`YYYY-MM-DDTHH:MM`) for the next expected mode change |
| `reserved_kwh` | float | Currently `0.0` in the summer strategy |
| `required_load_kwh` | float | Currently `0.0` in the summer strategy |
| `charge_source` | str \| null | Currently `null` in the summer strategy |

When `exclude_from_recording` is enabled for the integration, the whole sensor
is excluded from recorder/history. Even when recorder is enabled for the
sensor, the large `charge_entries` attribute is excluded from recorder
attribute storage.

---

## Algorithm

The schedule is recomputed whenever the linked price sensor changes (i.e. when
new Nordpool prices arrive). The current temporary summer strategy is simple:

1. **Parse the compact price schedule** into local start/end datetimes.
2. **Default every slot to `maxuse`**.
3. **Pick sell candidates** whose slot starts between `00:00-10:00` or `17:00-24:00`.
4. **Rank those candidates by export value** (`credit`, falling back to `cost`).
5. **Mark the top six candidate slots per local day as `sell`**. Ties are kept deterministic by preserving chronological order.

A background task (`_periodic_update`) wakes at each slot boundary to advance the current mode and re-evaluate. The linked price sensor notifies the battery sensor when new rate data arrives, so the schedule is recomputed without relying on fixed entity IDs.

Rate changes are detected via a hash of `(from, cost, credit)` tuples to avoid redundant recomputation.

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
- `custom_components/energyadvisor/sensor/batterychargemodesensor.py` — all logic: algorithm functions + battery sensor entity
- `custom_components/energyadvisor/const.py` — `CONF_BATTERY_CAPACITY_KWH`, `CONF_BATTERY_MAX_CHARGE_POWER_W`, `CONF_BATTERY_DEGRADATION_COST`, `CONF_BATTERY_SOC_ENTITY`
- `tests/test_energyadvisor_battery_charge_mode_sensor.py` — ~600 lines, covers algorithm edge cases, config defaults, state transitions

---

## Notes

- Battery timing overrides are optional. If `battery_capacity_kwh` and `battery_max_charge_power_w` are both left empty, the integration falls back to the default 160-minute charge and 240-minute discharge timings.
- `battery_capacity_kwh` and `battery_max_charge_power_w` must be provided together when overriding the defaults.
- The current summer strategy does not apply SoC or solar-forecast constraints in the sensor itself. If battery export should stop above a specific floor, configure that limit in the battery/inverter.
- Battery timing and SoC-related config values are still stored so the richer planner can be brought back later without changing the config flow again.
