# Household Load Forecast Sensor

## Purpose

An optional sensor that currently exposes a fixed household Load forecast
profile while the quiet-night learning logic is being rebuilt.

The coordinator still keeps lifecycle housekeeping and persists a minimal state
under `.storage/energyadvisor_household_forecast_<entry_id>`.

---

## Input

### Required configuration

| Option | Key | Example |
|---|---|---|
| Household energy meter | `power_meter_consumption` | `sensor.household_energy_total` |
| Water heater active sensor | `water_heater_active_entity` | `binary_sensor.water_heater_active` |
| Central heating active sensor | `central_heating_active_entity` | `binary_sensor.central_heating_active` |

Configure these in **Settings -> Devices & Services -> Energy Advisor -> Configure**.

The sensor is only created when all three inputs are present.

---

## Output sensor

**Default entity ID:** `sensor.energy_advisor_load_forecast` for the first config
entry. Additional entries receive the usual Home Assistant numeric suffixes,
such as `sensor.energy_advisor_load_forecast_2`.

### State

Fixed household Load forecast value in `kW` (currently `0.5`).

Unit: `kW` | Device class: `power`

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `forecasts` | list[dict] | 192 entries for today+tomorrow (48 hours in 15-minute slots), each at `500 W` |
| `household_load_forecast_w` | float | Fixed value in watts (`500.0`) |
| `household_base_load_w` | float | Backward-compatible alias for `household_load_forecast_w` |
| `learning_nights` | int | Always `0` while learning is disabled |
| `data_since` | str \| null | Always `null` while learning is disabled |
| `last_sample_date` | str \| null | Always `null` while learning is disabled |
| `last_sample_kw` | float \| null | Always `null` while learning is disabled |
| `reason` | str | Human-readable status message |

The `reason` attribute currently reports:

`Household forecast learning is disabled; using a fixed 500 W profile.`
