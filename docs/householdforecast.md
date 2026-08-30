# Household Forecast Sensor

## Purpose

An optional sensor that learns the household's quiet-night base load from the
energy meter. It watches the cumulative household meter between 01:00 and
04:00, but only accepts the sample when the water heater and central heating
stay off for the whole window. The learned value is exposed as an average base
load in kW and is intended for the future battery reserve math.

The 01:00-04:00 quiet-night rule comes from the staged optimizer plan, and the
learned samples are persisted in Home Assistant storage so restarts do not
clear the value.

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

**Default entity ID:** `sensor.energy_advisor_base_load` for the first config
entry. Additional entries receive the usual Home Assistant numeric suffixes,
such as `sensor.energy_advisor_base_load_2`.

### State

The current rolling average household base load in `kW`.

Unit: `kW` | Device class: `power`

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `household_base_load_w` | float \| null | Rolling average base load in watts |
| `learning_nights` | int | Number of valid quiet-night samples in the average |
| `data_since` | str \| null | ISO date of the oldest retained sample |
| `last_sample_date` | str \| null | ISO date of the most recent sample |
| `last_sample_kw` | float \| null | Most recent sample in kW |
| `reason` | str | Human-readable status message |

---

## How it works

1. At 01:00 local time, the coordinator snapshots the household meter.
2. During the 01:00-04:00 window, any `on` event from the water-heater or
   central-heating binary sensors invalidates the sample.
3. At 04:00 local time, the coordinator snapshots the meter again.
4. If both quiet-night sensors stayed off and the meter never moved backwards,
   the sample is accepted.
5. The base-load sample is computed as `(meter_at_04:00 - meter_at_01:00) / 3`.
6. The sensor exposes the rolling average of all retained quiet-night samples.

When there are no valid samples yet, the sensor state is `unknown` and the
`reason` attribute explains that it is still waiting for the first quiet night.

Persisted data lives under `.storage/energyadvisor_household_forecast_<entry_id>`.
