# Household Load Forecast Sensor

## Purpose

An optional sensor that publishes a 48-hour household Load forecast in 15-minute
slots. The forecast is learned from the household meter history and is a
seasonal profile by slot-of-day and day type (workday / Saturday / Sunday).

**Target.** The forecast is the household's own base load, not the net grid
import/export. When appliance power sensors are configured, their measured power
is subtracted from the metered signal before the model is fitted, so the
water heater, central heating, pool pump and dehumidifier are not part of the
forecast. Each `load` value is the average power in kW over its 15-minute slot.

**Fallback.** While there is not enough usable history, the sensor publishes a
fixed 0.60 kW profile, so the full 192-slot `forecasts` list is always
available.

Besides the forecast, the sensor exposes diagnostics for stale, sparse or
invalid meter data (`quality_status`, `quality_warnings`,
`last_valid_required_sample`, `reason`).

---

## Input

Configure the inputs in **Settings -> Devices & Services -> Energy Advisor ->
Configure**. The household inputs are spread over these steps: *Battery
hardware*, *Grid metering*, *Household load*, *Household activity filters*,
*Water heater* and *Flexible loads*.

### Required configuration

| Option (label) | Key | Step | Example |
|---|---|---|---|
| Household energy meter (cumulative kWh) | `power_meter_consumption` | Household load | `sensor.household_energy_total` |

The sensor and its coordinator are only created when this key is set. The meter
must report the gross household consumption. Accepted units are energy counters
(`Wh`, `kWh`, `MWh`) and power sensors (`W`, `kW`, `MW`); power samples are
integrated over time. `mW`/`mWh` are treated as milli-units. If no sensor
reports gross household demand, use a derived/template sensor.

### Optional configuration that influences the forecast

These inputs are subtracted from the meter signal.

| Option (label) | Key | Step | Example | Role |
|---|---|---|---|---|
| Water heater power sensor | `water_heater_power_entity` | Water heater | `sensor.water_heater_power` | Measured power, subtracted from the meter signal |
| Water heater rated power | `water_heater_power_w` | Water heater | `3500` | Fallback power in W; used only together with the active sensor when the power sensor has no usable history |
| Water heater active sensor | `water_heater_active_entity` | Household activity filters | `binary_sensor.water_heater_active` | Fallback timing; the heater counts as drawing the rated power while the sensor is active and the power sensor has no usable history |
| Central heating power sensor | `central_heating_power_entity` | Household activity filters | `sensor.central_heating_power` | Measured power, subtracted from the meter signal |
| Central heating rated power | `central_heating_power_w` | Household activity filters | `2200` | Fallback power in W; used only together with the active sensor when the power sensor has no usable history |
| Central heating active sensor | `central_heating_active_entity` | Household activity filters | `binary_sensor.central_heating_active` | Fallback timing, as for the water heater |
| Pool pump power sensor | `pool_pump_power_entity` | Flexible loads | `sensor.pool_pump_power` | Measured power, subtracted from the meter signal |
| Dehumidifier power sensor | `dehumidifier_power_entity` | Flexible loads | `sensor.dehumidifier_power` | Measured power, subtracted from the meter signal |

Appliance power sensors accept the same units as the meter. The fallback only
applies to the water heater and the central heating, because only they have an
active sensor. Rated-power values must be at least 1 W.

### Optional configuration that is currently not in use

These keys are stored in the config entry and, where noted, their history is
recorded in the SQLite database. None of them influences the forecast or any
sensor attribute value.

| Option (label) | Key | Step | Example | Status |
|---|---|---|---|---|
| Pool pump rated power | `pool_pump_power_w` | Flexible loads | `500` | currently not in use (the value is read, but it is only a fallback for an active sensor, which the pool pump does not have) |
| Dehumidifier rated power | `dehumidifier_power_w` | Flexible loads | `1500` | currently not in use (same reason as the pool pump) |
| Water heater worst-case refill hours | `water_heater_max_hours` | Water heater | `4` | currently not in use (not read by the household forecast) |
| Bathroom humidity sensor | `bathroom_humidity_entity` | Water heater | `sensor.bathroom_humidity` | currently not in use (history is recorded but not used) |
| Outdoor temperature sensor | `outdoor_temperature_entity` | Household load | `sensor.outdoor_temperature` | currently not in use (history is recorded but not used; the slot feature `outdoor_temp_c` is always `null`) |
| Battery state of charge sensor | `battery_soc_entity` | Battery hardware | `sensor.battery_soc` | Used by the battery charge mode sensor as the start of each plan (history is also recorded) |
| Battery charge power sensor | `battery_charge_power_entity` | Battery hardware | `sensor.battery_charge_power` | currently not in use (history is recorded but not used) |
| Grid import power sensor | `grid_import_entity` | Grid metering | `sensor.grid_import_power` | currently not in use (history is recorded but not used) |
| Grid export power sensor | `grid_export_entity` | Grid metering | `sensor.grid_export_power` | currently not in use (history is recorded but not used) |

### Raw capture

Every configured entity from the lists above (meter, power sensors, active
sensors, humidity, outdoor temperature, battery and grid sensors) is captured
into the SQLite database:

- on every state change, and
- by a watchdog poll every 60 s for the meter and every 120 s for all other
  entities. A poll stores an unchanged value as a `heartbeat` sample only once
  the last stored sample is 120 s old.

Numeric entities are stored in `raw_samples`. The two active sensors are stored
in `raw_events`; `on`, `true`, `active`, `heating`, `home` count as active and
`off`, `false`, `inactive`, `idle`, `standby` as inactive. A state change to
`unknown`, `unavailable` or an unparsable value is dropped and raises the
`invalid_sample_dropped` warning, also for entities that are otherwise not in
use.

---

## Output sensor

**Default entity ID:** `sensor.energy_advisor_load_forecast` (name "Load
forecast") for the first config entry. Additional entries receive the usual
Home Assistant numeric suffixes, such as `sensor.energy_advisor_load_forecast_2`.

The sensor is registered by the sensor platform when `power_meter_consumption`
is set, together with the other Energy Advisor sensors (which need the
configured Nord Pool price sensor to resolve). Setup already runs one history
rebuild, so the first published state is not a placeholder when history exists.

### State

The `load` of the slot that covers the current local time, in `kW`, rounded to
3 decimals. This is not always `forecasts[0]`, because the list starts at local
midnight. During cold start it is `0.6`.

Unit: `kW` | Device class: `power` | No state class

Recorder: the `forecasts` attribute is never stored by the recorder. The
`exclude_from_recording` option is currently not in use (see the
[README](../README.md#recorder-and-history)).

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `forecasts` | list[dict] | 192 entries (48 h in 15-minute slots) from local midnight today to the end of tomorrow; each item is `{"from": "YYYY-MM-DDTHH:MM", "load": kW}` with `from` in local time without a timezone suffix |
| `last_forecast_generation` | str | Local `YYYY-MM-DDTHH:MM` time of the latest quarter-hour refresh |
| `quality_status` | str | `ok`, `degraded`, `stale` or `fallback` (see below) |
| `quality_warnings` | list[str] | Sorted warning codes; empty when the forecast is healthy |
| `last_valid_required_sample` | str \| null | Local `YYYY-MM-DDTHH:MM` time of the latest stored meter sample; `null` if none |
| `household_load_forecast_w` | float | Current-slot forecast in W, rounded to 0.1 (`600.0` during cold start) |
| `household_base_load_w` | float | Backward-compatible alias for `household_load_forecast_w` |
| `learning_nights` | int | Number of distinct local days with usable slot history (up to 180 days); `0` during cold start |
| `data_since` | str \| null | Local date (`YYYY-MM-DD`) of the oldest usable slot |
| `last_sample_date` | str \| null | Local date (`YYYY-MM-DD`) of the newest usable slot |
| `last_sample_kw` | float \| null | Load of the newest usable closed slot in kW, rounded to 3 decimals |
| `reason` | str | Human-readable status message (see below) |

`data_since`, `last_sample_date` and `last_sample_kw` are `null` during cold
start.

### Forecast slots

- 192 physical 15-minute slots; slot 0 starts at local midnight of the current
  day, slot 191 at 23:45 tomorrow. The anchor moves to the new day at midnight.
- Slots before the current one are frozen: refreshes within the same day keep
  their published `load` values and only rewrite the current and future slots.
- `load` is never negative and is rounded to 3 decimals.

### Quality status

`quality_status` and `quality_warnings` are recalculated at setup and on every
quarter-hour refresh.

| Status | Meaning |
|---|---|
| `fallback` | Fewer than 24 usable slots; the fixed 0.60 kW profile is published. Warning: `insufficient_history` |
| `degraded` | A forecast is published, but a warning is active (for example `warming_up` during the first 7 days) |
| `stale` | The latest stored meter sample is older than 120 s but younger than 30 min |
| `ok` | Learned model (7 or more days), no warnings |

| Warning code | Raised when |
|---|---|
| `insufficient_history` | Fewer than 24 usable slots exist |
| `warming_up` | At least 24 usable slots but fewer than 7 learned days |
| `required_meter_stale` | Latest stored meter sample is older than 120 s (status `stale`) |
| `required_meter_outage` | Latest stored meter sample is 30 min or older (status `degraded`) |
| `required_meter_missing` | History exists but no meter sample has been stored |
| `sparse_sampling` | One of the last 8 closed slots of today has no samples or a quality score below 0.9 |
| `invalid_sample_dropped` | A sample or interval was dropped (unparsable state, missing or mismatched unit, counter reset of 0.5 kWh or more, or an interval above 50 kW). Kept for 24 h and persisted across restarts |

### Reason

`reason` is one of the following base messages, where `N` is `learning_nights`:

- `Household forecast is in cold-start mode; using a fixed 600 W profile.`
- `Household forecast is warming up; using a recency-weighted baseline from N learned days of slot history.`
- `Household forecast is using a seasonal baseline learned from N days of slot history.`

A learned or warming-up message is prefixed in these cases (the first matching
one is used):

- stale: `Household forecast is stale; required household meter last updated at <time>. <base message>`
- degraded: `Household forecast is degraded because <cause>. <base message>`, where
  `<cause>` is, in priority order: `the required household meter last updated
  at <time>` (outage), `the required household meter is unavailable`,
  `recent meter intervals were sparse`, or `one or more invalid samples were
  dropped`. With only the `warming_up` warning the base message is shown
  unchanged.

The coordinator itself can also report `Household forecast is waiting for the
required household meter.` when no meter is configured; the sensor is not
created in that case.

---

## How it works

### 1. Capture and persistence

Raw samples are written to a SQLite file next to the Home Assistant storage:
`<config>/.storage/energyadvisor_household_forecast_<entry_id>.db` (WAL mode,
schema version 1).

| Table | Content | Retention |
|---|---|---|
| `raw_samples` | Numeric samples per config key | 21 days |
| `raw_events` | Active-sensor states per config key | 21 days |
| `interval_energy` | Meter energy per interval between two samples | 21 days |
| `slot_rows` | Closed 15-minute slots: net energy, `load_kw`, sample count, quality score, `features_json` | 180 days |
| `forecast_runs` | Every published 192-slot forecast | 14 days |
| `meta` | Checkpoints (`last_finalized_slot_utc`, `last_generation_utc`, `last_raw_prune_utc`), `meter_entity_id`, `quality_warning_state` | - |

`features_json` of a slot also stores the gross and subtracted appliance
energy, `outdoor_temp_c` (always `null`) and the latest water-heater and
central-heating active flags; the model does not read the last two back.

Old rows are pruned once per day. The separate Home Assistant store file
`.storage/energyadvisor_household_forecast_<entry_id>` only holds a minimal
static marker (`mode`, `load_forecast_kw`) and is rewritten only when a legacy
payload is normalized.

If the configured meter entity differs from the one stored in `meta`, the meter
samples, interval energy, slot rows, forecast runs and checkpoints are deleted,
because the new entity has an unrelated counter. Appliance samples are kept.

### 2. Intervals and slots

1. Consecutive meter samples become one interval of `delta_kwh`. Power samples
   use the trapezoid rule; energy counters use the difference. A decrease is
   dropped (a decrease of 0.5 kWh or more also raises `invalid_sample_dropped`),
   as is an interval whose average power exceeds 50 kW.
2. Intervals longer than 300 s are marked `sparse_gap` but still allocate their
   energy.
3. The energy of an interval is split proportionally over the 15-minute slots it
   overlaps. The same is done for the appliance intervals.
4. For each closed slot,
   `load_kw = max(0, gross_kwh * 900 / observed_seconds - appliance_kwh) / 0.25`,
   so a partly observed slot keeps its true average power. `slot_energy_kwh` is
   the measured net energy.
5. `quality_score` is the observed share of the slot (0-1), reduced by up to
   50 % when sparse intervals contributed. A slot without samples scores 0.

Appliance energy is computed per power sensor with the same interval logic. For
the water heater and central heating, if the power sensor has no interval
history at all, the rated power (`*_power_w`) is applied while the matching
active sensor was on. A power sensor that is merely quiet in the rebuilt window
does not trigger this fallback.

### 3. Usable history

Slots are excluded from training when their quality score is 0, when the
load is not finite, negative or above 50 kW, or when they are stalled: the slot
was observed (quality score of at least 0.35) but the meter reported at most
0.001 kWh, so it was not counting. A local day with at least 24 slot rows of
which 50 % or more are stalled is excluded completely. Up to 180 days of slot
rows are considered.

### 4. Model

| Mode | Condition | Forecast |
|---|---|---|
| Cold start | Fewer than 24 usable slots | Constant 0.60 kW |
| Warm-up | At least 24 usable slots, fewer than 7 distinct days | Recency-weighted trimmed mean per slot-of-day (day types are not separated yet) |
| Learned | 7 or more distinct days | Seasonal profile per (day type, slot-of-day) |

- Training uses the last 56 days (by time; if no row falls in that window, the
  newest 56 x 96 rows). With at least 100 rows, rows above
  `max(5 kW, 3 x P99)` are ignored.
- Each row is weighted by its quality score and a recency factor
  `0.5 ** (age_days / 21)`.
- Each bucket is a weighted mean with 10 % of the weight trimmed from each end
  once it has at least 5 samples.
- Day types are `workday` (Mon-Fri), `saturday` and `sunday`; holidays are not
  handled.
- Bucket fallback: the (day type, slot-of-day) bucket when it has at least 2
  samples (learned mode only), then the slot-of-day bucket, then the day-type
  bucket, then the global mean.
- **Trend (learned mode):** the mean deviation of the last 8 usable slots from
  their own baseline, clamped to +/-1.5 kW, is added to the current and future
  slots with weight 0.35 and an exponential fade of `exp(-slots_ahead / 24)`.
  It is skipped when the newest slot ended more than 60 minutes ago.

### 5. Residual correction

Once at least 7 days are learned, a short-term correction nudges the current
slot and the following 7 slots. It uses the closed slots of today (at most the
last 8) that have samples and a quality score of at least 0.5. At least 2
consecutive closed slots must differ from the uncorrected forecast by 0.25 kW
or more. The slot errors are smoothed with an EMA (alpha 0.35), clamped to
+/-1.5 kW and applied with a decay of 0.82 per slot. The correction is 0 when
the newest closed slot ended 30 minutes or more before the current slot, and
for the first forecast build after a restart (the uncorrected forecast it is
compared with is kept in memory only).

### 6. Refresh cadence

- State changes and the 60 s watchdog poll keep the raw tables current.
- At `:00`, `:15`, `:30` and `:45` the coordinator sets
  `last_forecast_generation`, notifies the sensor, and rebuilds in the
  background: new meter samples become intervals and closed slot rows, the
  forecast is recomputed and stored in `forecast_runs`, and the sensor is
  updated again.
- The first rebuild after startup covers all retained samples. Later rebuilds
  only redo the slots from 8 slots before the last finalized slot onward.
- On startup the latest stored forecast is restored first and then rebuilt.
- During a meter outage the forecast keeps publishing from stored history; the
  outage shows up in `quality_status`, `quality_warnings` and `reason`.
