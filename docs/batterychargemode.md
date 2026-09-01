# Battery Charge Mode Sensor

> **Scope note:** this document describes the **Energy Advisor** battery helper.

## Purpose

The battery helper computes a price-aware schedule for a home battery. When
the optimizer is enabled and the configured SoC sensor is available, it uses
the HiGHS solver through the `highspy` Python bindings to choose charge,
discharge, sell, and idle periods over the requested horizon. If a solar
forecast is configured, it also reserves battery headroom for forecast solar
production. If optimization is disabled, or the solver stack is not
available, it falls back to the legacy price schedule.

The current optimizer considers electricity prices and forecast solar headroom
only. It does not use load forecasts, weather, or battery degradation costs
yet.

## Input

### Required for optimization

- The linked price sensor for the same config entry.
- `battery_soc_entity` so the current battery SoC can be read.
- `battery_capacity_kwh`
- `battery_max_charge_power_w`
- `battery_max_discharge_power_w` or, if omitted, the configured charge power
  is used as the discharge limit.
- `battery_optimization_enabled`
- `battery_optimization_horizon_hours`
- `battery_min_soc_pct`
- `battery_max_soc_pct`

### Optional and stored for later planner stages

The config flow also stores:

`battery_charge_power_entity`, `grid_import_entity`, `grid_export_entity`,
`outdoor_temperature_entity`, `household_load_forecast_w`,
`water_heater_power_entity`, `water_heater_power_w`, `water_heater_max_hours`,
`bathroom_humidity_entity`, `pool_pump_power_entity`, `pool_pump_power_w`,
`dehumidifier_power_entity`, and `dehumidifier_power_w`.

The solar forecast sensor is used automatically when configured so the battery
optimizer can leave room for forecast PV production. The remaining fields are
not used by the current optimizer yet.

## Configuration

Set during the **battery** step of the initial setup flow or later via
**Settings → Devices & Services → Energy Advisor → Configure**.

| Option | Key | Default | Description |
|---|---|---|---|
| Battery capacity | `battery_capacity_kwh` | — | Usable battery capacity in kWh. |
| Max charge power | `battery_max_charge_power_w` | — | Maximum battery charge power in W. |
| Max discharge power | `battery_max_discharge_power_w` | `battery_max_charge_power_w` | Maximum discharge power in W. |
| Enable optimization | `battery_optimization_enabled` | `false` | Turns the LP optimizer on or off. |
| Optimization horizon | `battery_optimization_horizon_hours` | `48` | Look-ahead horizon in hours. |
| Minimum SoC | `battery_min_soc_pct` | `5` | Lower SoC bound in percent. |
| Maximum SoC | `battery_max_soc_pct` | `95` | Upper SoC bound in percent. |
| Battery SoC sensor | `battery_soc_entity` | — | Current battery state of charge sensor. |

## Output sensor

**Default entity ID:** `sensor.energy_advisor_battery_charge_mode` for the
first config entry. Additional entries receive the usual Home Assistant numeric
suffixes.

### State

The sensor state is the current schedule mode:

- `standby` — no battery flow is active and the battery is effectively idle.
- `maxuse` — battery energy is held ready for self-consumption.
- `charge` — the optimizer wants the battery SoC to rise.
- `discharge` — the optimizer wants the battery SoC to fall to cover household load.
- `sell` — the optimizer wants the battery SoC to fall while exporting.

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `modes` | list[dict] | Sequential schedule entries, one per 15-minute input slot, with local `from`, `mode`, and optional `target_soc`. |
| `current_soc_pct` | float \| null | Current SoC read from the configured battery sensor. |
| `current_target_soc` | float \| null | Target SoC for the active schedule segment, if applicable. |
| `optimization_enabled` | bool | Whether the LP optimizer is enabled. |
| `reason` | str | Human-readable explanation for the current recommendation. |
| `solver` | str \| null | Solver used for the latest optimization run, or `null` when falling back. |

## Algorithm

1. Normalize the price sensor rates into local-time slots within the requested horizon.
2. Normalize the solar forecast into the same horizon and compute the battery headroom that should be preserved for future PV production.
3. Build a linear program with charge, discharge, and sell decision variables.
4. Constrain SoC between the configured minimum and maximum bounds.
5. Respect the configured charge and discharge power limits.
6. Minimize import cost while accounting for avoided import cost when discharging, export credit when selling, the solar headroom reserve, and the value of the remaining SoC at the end of the horizon.
7. Return the solved result as one schedule entry per 15-minute input slot.

When optimization is disabled, the helper falls back to the legacy schedule:

- `maxuse` by default.
- `sell` during the highest-value morning/evening candidate slots from the price data.

## Architecture

```
PriceSensor
    │
    └─► BatteryChargeModeSensor
              │
              └─► battery_optimizer.optimize_battery_schedule()
                        │
                        ├── HiGHS available?         →  optimized schedule
                        └── otherwise                →  legacy price schedule
```

The battery sensor recomputes the schedule when the price sensor updates.
When optimization is enabled and a SoC sensor is configured, it also listens
for SoC changes so the active recommendation updates immediately. If the solar
forecast sensor is configured, the helper also updates when the solar forecast
changes so the reserved headroom stays in sync.

## Notes

- The current optimizer does not use load forecasts, weather, or degradation
  costs yet.
- If the battery SoC sensor is unavailable, the helper falls back to the legacy
  schedule.
- The optimizer values the remaining battery SoC at the horizon end so it does
  not prefer a sell/rebuy cycle when the spread is too small.
- The schedule entries are advisory only; automations remain responsible for
  actually controlling the battery.
