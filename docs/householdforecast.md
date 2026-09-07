# Household Load Forecast Sensor

## Purpose

An optional sensor that exposes a seasonal household Load forecast learned
from retained slot history. While the model is still warming up, it falls back
to a fixed 0.60 kW cold-start profile so HA always gets a full 192-slot
contract.

The coordinator still keeps lifecycle housekeeping and persists a minimal state
through Home Assistant storage, while raw samples, interval-energy rows,
closed slot rows, and forecast checkpoints are written to
`.storage/energyadvisor_household_forecast_<entry_id>.db`.
The forecast shell is anchored to local midnight and refreshes on quarter-hour
boundaries without moving already-published historical slots. The
`last_forecast_generation` attribute advances on each refresh so HA shows the
update when the forecast changes or refreshes.

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

Current household Load forecast value in `kW`. During cold start this is
`0.60`; once enough slot history is available it becomes the learned seasonal
value for the first slot in the current 48-hour horizon.

Unit: `kW` | Device class: `power`

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `forecasts` | list[dict] | 192 entries for the 48-hour horizon in 15-minute slots; each item has local `from` and `load` fields anchored to the current day |
| `household_load_forecast_w` | float | Current forecast in watts (`600.0` during cold start) |
| `household_base_load_w` | float | Backward-compatible alias for `household_load_forecast_w` |
| `learning_nights` | int | Number of retained learned days used for the seasonal model |
| `data_since` | str \| null | Oldest retained learned date |
| `last_sample_date` | str \| null | Most recent learned sample date |
| `last_sample_kw` | float \| null | Most recent learned sample in kW |
| `last_forecast_generation` | str | Local `YYYY-MM-DDTHH:MM` timestamp of the latest republish |
| `reason` | str | Human-readable status message describing cold start, warm-up, or learned baseline mode |

The `reason` attribute currently reports one of:

- `Household forecast is in cold-start mode; using a fixed 600 W profile.`
- `Household forecast is warming up; using a recency-weighted baseline from N learned days of slot history.`
- `Household forecast is using a seasonal baseline learned from N days of slot history.`
