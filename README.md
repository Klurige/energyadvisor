# Energy Advisor for Home Assistant

A custom component for Home Assistant that provides Energy Advisor price sensors based on data from the Home Assistant NordPool integration. This integration helps you monitor and automate your home based on real-time and forecasted electricity prices, using the NordPool sensor as a data source. It also provides optional household load and solar production forecasts, and a battery charge schedule that plans when to charge and discharge a home battery from the electricity prices.

This integration works particularly well with the [LevelIndicatorClock](https://github.com/Klurige/LevelIndicatorClock), allowing you to visualize price levels throughout the day in an analogue clock.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/klurige)

---

## Features
- Uses electricity prices provided by the Home Assistant NordPool integration.
- Categorizes prices into levels (e.g., low, medium, high).
- Provides price and battery mode sensors with compact diagnostics, a water-heater schedule and diagnostic sensor, an optional household Load forecast sensor, and an optional refined solar forecast sensor.
- Supports multiple languages (English, Swedish).
- Easy configuration via the Home Assistant UI.
  - Adds support for extra fees and taxes from your grid and supplier.
  - Allows for setting thresholds for low and high prices.
  - Adds support for credits when exporting electricity.
- Provides a ranking system for prices to help identify the best times to use electricity.
- Exposes a household Load forecast that falls back to a cold-start shell and
  learns a seasonal baseline from retained slot history.
- Can refine a solar production forecast using your solar's measured output.
- Plans a battery schedule from the prices, the household load forecast and the battery state of charge: `charge` in the cheap hours before a price peak, `discharge` to cover the household load during the peak, `sell` to export battery energy when the credit pays for it and `standby` elsewhere (see [Planner](#planner)).

## Prerequisites
- Home Assistant (2025.0 or newer recommended)
- [NordPool integration](https://www.home-assistant.io/integrations/nordpool/) installed and configured in Home Assistant. This integration supplies the electricity prices that this component depends on.

## Installation

### Option 1: HACS (Recommended)

1. Open Home Assistant and go to HACS.
2. Search for `Energy Advisor` in Integrations.
3. Install the repository.
4. Restart Home Assistant.

Or use the direct link:
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Klurige&repository=energyadvisor&category=integration)

#### Add the integration via the Home Assistant UI
1. Go to `Settings` -> `Devices & Services`
2. Select `+ Add Integration`
3. Search for `Energy Advisor`
4. Select your Nord Pool current-price sensor and press `Submit`
5. Step through the remaining pages (fees, credits, taxes, thresholds, and the optional solar, battery and household inputs; see [Configuration](#configuration)). Only the Nord Pool sensor is required; leave anything you do not need empty.

### Option 2: Manual
1. Copy the `custom_components/energyadvisor` directory into your Home Assistant `custom_components` folder.
2. Restart Home Assistant.
3. Add the integration via the Home Assistant UI.

## Configuration
The integration is configured through the Home Assistant UI. Only the Nord Pool prices sensor is required; every other field is optional and can be left empty. To change the settings later, open `Settings` -> `Devices & Services` -> `Energy Advisor` -> `Configure`. Saving reloads the integration.

The setup pages come in this order: *Price source*, *Supplier fees and credits*, *Grid fees and credits*, *Taxes and VAT*, *Price thresholds*, *Solar forecast*, *Battery hardware*, *Battery optimization*, *Grid metering*, *Household load*, *Household activity filters*, *Water heater* and *Flexible loads*. When you reconfigure, the price source and thresholds share the first page.

There are many extra fees and taxes that can be added to the price. Suppliers and grid owners can have many different types of fees, which may vary by region or contract. You can specify the details of these fees in the `supplier_note` and `grid_note` fields, and add up the actual amounts in the corresponding fee fields. This allows you to document and sum up all relevant charges for your specific situation in the configuration.

I have added to the configuration taxes and fees added by my grid and supplier. They are no doubt
called differently for other grids and suppliers.

Inputs marked *currently not in use* are accepted and stored in the config entry, but they do not change any sensor value. They are kept for later features. Where noted, the household forecast also records their history.

#### Price, fees and thresholds

All amounts are in the currency of the Nord Pool sensor, per kWh. Percentages are entered as whole numbers (`5` means 5 %).

| Key                        | Description                  | Example Value | Status |
|----------------------------|------------------------------|---------------|--------|
| `nordpool_prices_sensor`   | Nord Pool prices sensor entity id (required) | `sensor.nordpool_kwh_se3_sek_3_10_025` | In use |
| `low_threshold`            | Prices with a total cost below this are `Low` | 0.8 | In use |
| `high_threshold`           | Prices with a total cost above this are `High` | 1.5 | In use |
| `supplier_note`            | Free-text note about the supplier fees; not used in any calculation | "My supplier fees" | Note only |
| `supplier_fixed_fee`       | Supplier fee added per imported kWh | 0.12 | In use |
| `supplier_variable_fee`    | Supplier fee in % of the spot price | 5 | In use |
| `supplier_fixed_credit`    | Supplier credit received per exported kWh | 0.00 | In use |
| `supplier_variable_credit` | Supplier credit in % of the spot price | 0 | In use |
| `grid_note`                | Free-text note about the grid fees; not used in any calculation | "My grid fees" | Note only |
| `grid_fixed_fee`           | Grid fee added per imported kWh | 0.20 | In use |
| `grid_variable_fee`        | Grid fee in % of the spot price | 10 | In use |
| `grid_fixed_credit`        | Grid credit received per exported kWh | 0.00 | In use |
| `grid_variable_credit`     | Grid credit in % of the spot price | 0 | In use |
| `grid_energy_tax`          | Energy tax added per imported kWh | 0.45 | In use |
| `electricity_vat`          | VAT in % applied to the import cost after fees and taxes | 25 | In use |
| `exclude_from_recording`   | Not shown in the UI; defaults to `true`. See [Recorder and history](#recorder-and-history) | `true` | currently not in use |
| `debug`                    | Only on the options page (default off). Takes prices from elprisetjustnu.se instead of Nord Pool and enables `energyadvisor.debug_select_date`. See [Debug mode](#debug-mode) | `false` | In use |

#### Solar forecast

| Key                        | Description                  | Example Value | Status |
|----------------------------|------------------------------|---------------|--------|
| `forecast_entity`          | Raw solar forecast sensor for today. Set together with `power_entity` | `sensor.home_energy_production_today` | In use |
| `power_entity`             | Solar actual power sensor in watts. Set together with `forecast_entity` | `sensor.solar_active_power` | In use |
| `forecast_tomorrow_entity` | Raw solar forecast sensor for tomorrow; has no effect without the two keys above | `sensor.home_energy_production_tomorrow` | In use |

See [docs/solarforecast.md](docs/solarforecast.md) for details.

#### Battery

Battery capacity and max charge power must be set together. Min SoC must be lower than max SoC. The battery charge mode sensor plans with these options (see [Planner](#planner)); without a capacity and a max charge power it keeps every slot on `standby`. Selling battery energy to the grid is part of the planning whenever a battery is configured; it needs no option of its own.

| Key                        | Description                  | Example Value | Status |
|----------------------------|------------------------------|---------------|--------|
| `battery_capacity_kwh`     | Usable battery capacity; set together with max charge power | `10.0` | In use (battery charge mode sensor) |
| `battery_max_charge_power_w` | Maximum battery charge power; set together with capacity | `5000` | In use (battery charge mode sensor) |
| `battery_max_discharge_power_w` | Maximum battery discharge power; the form pre-fills it with the max charge power, and the planner uses the max charge power when it is unset. It also limits how fast the battery can export (`sell`) | `5000` | In use (battery charge mode sensor) |
| `battery_optimization_horizon_hours` | Look-ahead horizon in hours (1-48, form default 48); the form field and the stored value are kept | `48` | currently not in use (the planner plans every known price slot) |
| `battery_min_soc_pct`      | Lower SoC bound in % (form default 5) | `5` | In use (battery charge mode sensor) |
| `battery_max_soc_pct`      | Upper SoC bound in % (form default 100) | `100` | In use (battery charge mode sensor) |
| `battery_degradation_cost` | Wear cost: cost per kWh delivered by the battery, in the same unit as the price sensor's `cost` (import price with fees, taxes and VAT), not the raw spot price. A cycle must save more than this and the round-trip losses before the planner uses it; no default (unset counts as `0`). Before this planner it was a minimum price spread | `0.7` | In use (battery charge mode sensor) |
| `battery_soc_entity`       | Battery state-of-charge sensor (percent); the start of every plan | `sensor.home_battery_soc` | In use (battery charge mode sensor; the household forecast also records its history) |
| `battery_charge_power_entity` | Battery charge power sensor | `sensor.home_battery_charge_power` | currently not in use (history is recorded but not used) |

#### Grid metering and household inputs

All of these are optional sensors. Only `power_meter_consumption` enables a feature: the household Load forecast sensor.

| Key                        | Description                  | Example Value | Status |
|----------------------------|------------------------------|---------------|--------|
| `grid_import_entity`       | Grid import power sensor | `sensor.grid_import_power` | currently not in use (history is recorded but not used) |
| `grid_export_entity`       | Grid export power sensor | `sensor.grid_export_power` | currently not in use (history is recorded but not used) |
| `power_meter_consumption`  | Whole-house energy meter (cumulative kWh) or power sensor. Creates the Load forecast sensor | `sensor.household_energy_total` | In use |
| `outdoor_temperature_entity` | Outdoor temperature sensor | `sensor.outdoor_temperature` | In use (heating forecast training; the household forecast only records its history) |
| `weather_entity` | Weather entity with temperature and wind forecast. Together with `central_heating_power_entity` it creates the Heating forecast sensor | `weather.home` | In use (heating forecast) |
| `wind_speed_entity` | Wind speed sensor; used to learn how wind increases the heating need | `sensor.wind_speed` | In use (heating forecast) |
| `water_heater_power_entity` | Water heater power sensor; its measured power is subtracted from the meter, and the water-heater schedule learns the heater's runtime from it | `sensor.water_heater_power` | In use (load forecast, water-heater schedule) |
| `water_heater_power_w`     | Water heater rated power in W; fallback for the load forecast when the power sensor has no history, and used for the schedule's expected cost | `3500` | In use (load forecast fallback, water-heater schedule) |
| `water_heater_active_entity` | Water heater active sensor; used with the rated power as fallback | `binary_sensor.water_heater_active` | In use (fallback) |
| `water_heater_max_hours`   | Worst-case water heater refill time in hours; the most the schedule plans per day | `4` | In use (water-heater schedule) |
| `bathroom_humidity_entity` | Bathroom humidity sensor; a day that reaches 100 % counts as a shower day | `sensor.bathroom_humidity` | In use (water-heater schedule; the household forecast only records its history) |
| `central_heating_power_entity` | Central heating power or energy sensor; its measured power is subtracted from the meter, and the heating forecast learns from it | `sensor.central_heating_power` | In use (load forecast, heating forecast) |
| `central_heating_power_w`  | Central heating rated power in W; fallback when the power sensor has no history, and the heating forecast's cold-start fallback | `2200` | In use (fallback) |
| `central_heating_active_entity` | Central heating active sensor; used with the rated power as fallback | `binary_sensor.central_heating_active` | In use (fallback) |
| `pool_pump_power_entity`   | Pool pump power sensor; its measured power is subtracted from the meter | `sensor.pool_pump_power` | In use |
| `pool_pump_power_w`        | Pool pump rated power in W | `500` | currently not in use |
| `dehumidifier_power_entity` | Dehumidifier power sensor; its measured power is subtracted from the meter | `sensor.dehumidifier_power` | In use |
| `dehumidifier_power_w`     | Dehumidifier rated power in W | `1500` | currently not in use |

When `power_meter_consumption` is configured, Energy Advisor also creates
`sensor.energy_advisor_load_forecast`, a 48-hour household load forecast in
15-minute slots. See [sensor.energy_advisor_load_forecast](#sensorenergy_advisor_load_forecast)
below and [docs/householdforecast.md](docs/householdforecast.md) for how the
inputs are used, the SQLite history file and the quality warnings.

When both `central_heating_power_entity` and `weather_entity` are configured,
Energy Advisor also creates `sensor.energy_advisor_heating_forecast`, a 48-hour
heating power forecast learned from outdoor temperature and wind. See
[sensor.energy_advisor_heating_forecast](#sensorenergy_advisor_heating_forecast)
below and [docs/heatingforecast.md](docs/heatingforecast.md).

#### Recorder and history

`exclude_from_recording` is currently not in use. It is copied to
`_attr_exclude_from_recording` on every sensor, but Home Assistant does not read
that attribute, so the sensor states are recorded as usual. To keep a sensor out
of the history, use the `exclude` option of the Home Assistant `recorder`
configuration.

Large attributes are always kept out of the recorder, regardless of any option:
- `rates` on `sensor.energy_advisor_price`
- `modes` on `sensor.energy_advisor_battery_charge_mode`
- `forecasts` on `sensor.energy_advisor_solar_forecast` and `sensor.energy_advisor_load_forecast`
- `on_slots` on `sensor.energy_advisor_water_heater_schedule`

#### Startup and reloads

Saving options or enabling an entity can make Home Assistant reload the entire
integration, briefly making its sensors unavailable. Household history rebuilding
runs in the background; the load forecast uses its cached forecast (or the
documented cold-start profile) until the rebuild finishes. Price and battery
sensors remain unavailable until a current price or calculated plan is ready,
rather than publishing a temporary zero price or `standby` command. The
water-heater schedule likewise waits for a restored or calculated plan.
Automations should ignore `unavailable` and `unknown` states instead of treating
them as commands to switch equipment.

For a battery-mode state trigger, guard both the mode and its target before
calling a battery control service. This also permits attribute-only updates
when the mode stays the same but `target_soc` changes:

```yaml
conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state is not none
         and trigger.to_state.state in
           ['charge', 'discharge', 'maxuse', 'pvcharge', 'sell', 'standby']
         and is_number(trigger.to_state.attributes.get('target_soc')) }}
```

## Usage
- The integration adds the price sensor, the compact levels sensor, one battery charge mode sensor and its compact diagnostic sensor, one water-heater schedule sensor and its diagnostic sensor, one optional household Load forecast sensor, one optional Heating forecast sensor, one optional solar forecast sensor, and one service. The default entity ids for the first config entry are `sensor.energy_advisor_price`, `sensor.energy_advisor_compact_levels`, `sensor.energy_advisor_battery_charge_mode`, `sensor.energy_advisor_compact_battery_modes`, `sensor.energy_advisor_water_heater_schedule`, `sensor.energy_advisor_water_heater_diagnostics`, `sensor.energy_advisor_load_forecast` when the household meter is configured, `sensor.energy_advisor_heating_forecast` when the heating power sensor and the weather entity are configured, and `sensor.energy_advisor_solar_forecast` when both solar forecast inputs are configured. Additional config entries receive numeric suffixes such as `sensor.energy_advisor_price_2`.
  - `sensor.energy_advisor_price` provides the current electricity price with all fees and taxes included, and a list of all known upcoming prices. (Nordpool gets the next day prices around 14:00 CET)
  - `sensor.energy_advisor_compact_levels` provides a compact level string intended for integrations such as Level Indicator Clock.
  - `sensor.energy_advisor_compact_battery_modes` provides the equivalent compact battery-mode string as a diagnostic sensor, disabled by default.
  - `sensor.energy_advisor_battery_charge_mode` provides a battery schedule with one entry per price slot: `charge` in the cheap slots before a price peak, `discharge` over the peak, `sell` when exporting pays, `standby` elsewhere, with the slot's `cost` and `credit` and the planned battery state of charge as `target_soc`. Its state is the mode of the current slot.
  - `sensor.energy_advisor_water_heater_schedule` provides a fixed daily 20:00→20:00 ON schedule for the water heater, using full-precision price costs and learned runtime demand (`no_shower` vs `shower` day classes). Its state is `on` when now is in a scheduled ON slot, else `off`.
  - `sensor.energy_advisor_water_heater_diagnostics` exposes the water-heater planner's diagnostic attributes.
  - `sensor.energy_advisor_load_forecast` provides a learned household load forecast in 15-minute slots for today and tomorrow. It publishes a fixed `0.60 kW` profile until enough meter history has been collected.
  - `sensor.energy_advisor_solar_forecast` provides a bias-corrected 15-minute solar production forecast based on your configured forecast and solar power sensors. The forecast inputs must be the raw 15-minute forecast sensors with `watts`, `wh_period`, or `forecasts` data; when a tomorrow forecast sensor is configured, updates from either forecast sensor rebuild the solar forecast.
  - `energyadvisor.get_levels` provides a string containing one character for each price level. (Level clock pattern. See https://github.com/Klurige/LevelIndicatorClock)
- Use these sensors in automations to optimize energy usage (e.g., run appliances when prices are low).

### `sensor.energy_advisor_price`
- **Description:** The current electricity price, including all configured fees and taxes.
- **State:** The total cost of the current price slot, rounded to 5 decimals, in `currency/kWh`.
- **Attributes:**
  - `spot_price`: The raw price from the NordPool sensor (before fees/taxes), normalised to currency per kWh.
  - `cost`: The total cost including all fees and taxes.
  - `credit`: The total credit received when exporting electricity.
  - `unit`: The energy unit from the selected Nord Pool prices sensor, for example `kWh` or `MWh`.
  - `currency`: The currency of the price, for example `SEK` or `EUR`. Sourced from the NordPool sensor.
  - `level`: Current price level as a string (`Low`, `Medium`, `High`, or `Unknown` when no price covers the current time).
  - `rank`: The current rank of the price compared to other prices for the current day. See the [Ranking](#ranking) section for details.
  - `low_threshold`: The threshold cost for low prices.
  - `high_threshold`: The threshold cost for high prices.
  - `rates`: A compact list of today's (and possibly tomorrow's) prices, each with:
    - `from`: The start time of the price period in local time (`YYYY-MM-DDTHH:MM`).
    - `cost`: The total cost for the period, including all fees and taxes (3 decimals).
    - `credit`: The total credit for the period (3 decimals).
    - `level`: The price level for the period as `L`, `M`, `H`, or `U`.
    - `rank`: The rank of the price for the period compared to other prices for the same day.
- **Update Frequency:** Updated when new Nord Pool data is processed and when the selected Nord Pool source sensor changes state.

See [docs/energyadvisor.md](docs/energyadvisor.md) for the price calculation and the data flow.

### `sensor.energy_advisor_compact_levels`
The integration also provides `sensor.energy_advisor_compact_levels`, which exposes electricity price levels in a compact format. It is intended for the Level Indicator Clock (https://github.com/Klurige/LevelIndicatorClock) and similar ESPHome-based clocks.
- **Default visibility:** Disabled by default in the entity registry.
- **Entity ID:** `sensor.energy_advisor_compact_levels`
- **State:** Minutes since midnight.
- **Attributes:**
  - `compact`: String containing `minutes_since_midnight:level_length:history:future` where `level_length` is in minutes and `history` and `future` are strings with one char per level, where:
    * `L` for Low
    * `M` for Medium
    * `H` for High
    * `U` for Unknown (if no price is available for that period)

    `history` covers up to one hour and `future` twelve hours, padded with `U` where no price is known.

### `sensor.energy_advisor_compact_battery_modes`

Diagnostic sensor, disabled by default in the entity registry, following the same
format as the compact price levels sensor.

- **State:** Minutes since local midnight.
- **Attribute `compact`:** `minutes_since_midnight:slot_length:history:future`,
  with slot length in minutes, one hour of history and twelve hours starting at
  the current slot. Missing slots are padded with `U`; before a schedule is
  available the format is `minutes_since_midnight:0::`.
- **Mode characters:** `S` = standby, `C` = charge, `P` = pvcharge,
  `D` = discharge, `E` = sell (export), `M` = maxuse, `U` = unknown.
- **Updates:** Follows every published battery plan, including SoC and forecast
  changes, independently of price updates. Rapid updates are coalesced to the
  latest plan and identical compact values are not written again. A single
  rescheduled timer advances the window at slot boundaries.

### `sensor.energy_advisor_solar_forecast`
- **Description:** Optional refined solar production forecast learned from the difference between your forecast source and the solar's measured output. Created when both `forecast_entity` and `power_entity` are configured.
- **Default Entity ID:** `sensor.energy_advisor_solar_forecast` for the first config entry.
- **State:** Corrected power estimate in `kW` for the current 15-minute slot.
- **Attributes:**
  - `forecasts`: 192 entries covering today and tomorrow in 15-minute steps, each with:
    - `end`: Slot end time in local time (`YYYY-MM-DDTHH:MM`).
    - `pow`: Corrected forecast power in `kW`.
    - `raw`: Uncorrected source forecast power in `kW`.
  - `energy_today_kwh`: Corrected energy estimate for today.
  - `energy_tomorrow_kwh`: Corrected energy estimate for tomorrow.
  - `total_samples`: Number of learned `(forecast, actual)` samples stored.
  - `data_since`: Oldest learned sample date.
  - `intraday_scaling`: Real-time scaling factor applied to today's remaining forecast.

See [docs/solarforecast.md](docs/solarforecast.md) for the full solar forecast description, correction model, and database behavior.

### `sensor.energy_advisor_load_forecast`
- **Description:** Optional household load forecast, learned from the history of your whole-house meter. Appliance power sensors that you configure (water heater, central heating, pool pump, dehumidifier) are subtracted first, so the forecast is the base load of the house. Created when `power_meter_consumption` is configured.
- **Default Entity ID:** `sensor.energy_advisor_load_forecast` for the first config entry.
- **State:** Forecast load in `kW` for the current 15-minute slot.
- **Attributes:**
  - `forecasts`: 192 entries covering today and tomorrow in 15-minute steps, each with `from` (slot start in local time, `YYYY-MM-DDTHH:MM`) and `load` (average power in `kW`). Slots that have already started keep their published values.
  - `last_forecast_generation`: Local time of the latest quarter-hour refresh.
  - `quality_status`: `ok`, `degraded`, `stale` or `fallback`.
  - `quality_warnings`: Warning codes that explain a status other than `ok`.
  - `last_valid_required_sample`: Local time of the latest stored meter sample.
  - `household_load_forecast_w` and `household_base_load_w`: Current-slot forecast in W.
  - `learning_nights`, `data_since`, `last_sample_date`, `last_sample_kw` and `reason`: Learning progress and a readable status message.
- **Cold start:** Until enough usable history exists (24 slots) the sensor publishes a fixed `0.60 kW` profile with `quality_status` `fallback`. After a week of history it uses a seasonal profile per slot of day and day type (workday, Saturday, Sunday).
- **Update Frequency:** Refreshed on every quarter-hour boundary. History is kept in `.storage/energyadvisor_household_forecast_<entry_id>.db`.

See [docs/householdforecast.md](docs/householdforecast.md) for the model, quality warnings and database tables.

### `sensor.energy_advisor_heating_forecast`
- **Description:** Optional space-heating power forecast. A change-point regression on outdoor temperature and wind, with a second slope for direct-electric backup, is trained on the recorder statistics and applied to the weather forecast. Created when `central_heating_power_entity` and `weather_entity` are configured.
- **State:** Forecast heating power in `kW` for the current 15-minute slot.
- **Attributes:** `forecasts` (192 slots with `from` and `load`), `energy_today_kwh`, `energy_tomorrow_kwh`, `quality_status`, `quality_warnings`, `reason`, `model` and calibration diagnostics.

See [docs/heatingforecast.md](docs/heatingforecast.md) for the model, the inputs and the quality warnings.

### `sensor.energy_advisor_battery_charge_mode`
- **Description:** Battery schedule built from the linked `sensor.energy_advisor_price` entry. A planner decides for every price slot whether the battery should charge from the grid, discharge into the household, sell to the grid or stand by, from the prices, the household load forecast and the battery state of charge (see [Planner](#planner)).
- **Default Entity ID:** `sensor.energy_advisor_battery_charge_mode` for the first config entry.
- **State:** The mode of the slot that contains the current time (`standby`, `charge`, `discharge`, `sell`, or `maxuse` in place of daytime `discharge` when solar storage beats export); `standby` when no slot contains it. The icon follows the mode: `mdi:battery-off`, `mdi:battery-arrow-up`, `mdi:battery-arrow-down`, `mdi:transmission-tower-export` or `mdi:battery-sync`.
- **Attributes:**
  - `modes`: One entry per price slot, in the same order and with the same start and end times as the `rates` attribute of `sensor.energy_advisor_price`. Each entry has the local slot start `from`, the slot end `to`, `mode` (one of the [modes](#battery-charge-modes) below; `maxuse` replaces daytime `discharge` only when solar storage beats export), `target_soc` (the planned battery state of charge at the end of the slot in whole percent, always rounded up so the battery keeps the higher level; `50` for every slot while the battery is not configured), and `cost` and `credit` copied from the same price slot (rounded to 2 decimals).
  - `target_soc`: The `target_soc` of the slot that contains the current time, in whole percent (rounded up): the planned battery state of charge at the end of the current slot (`50` while the battery is not configured). It is always present, and `null` only when no slot contains the current time. It is not the measured state of charge; that stays available from the `battery_soc_entity` itself.

### `sensor.energy_advisor_water_heater_schedule`
- **Description:** Fixed plan generated once per day at local 20:00 for the next 24 hours (to next 20:00), minimizing expected electricity cost from valid 15-minute price slots. When there is no plan for the current window (first start, or Home Assistant was not running at 20:00), a plan for the time left in the window is made right away. A plan that found no prices is retried every 5 minutes; a plan with slots is never changed before the window ends.
- **State:** `on` if now falls in a planned ON slot, otherwise `off`.
- **Required config:** `water_heater_power_entity`, `bathroom_humidity_entity`, `water_heater_power_w` (rated power), `water_heater_max_hours`.
- **Learning model:** Two weekday-aware runtime models (`no_shower`/`shower`), 30-day rolling window, 95th percentile (nearest-rank), sparse fallback hierarchy, default/blended/learned sample handling (default below 10 samples, blend 10-20, learned at 20+).
- **Threshold detection:** Deterministic histogram+Otsu split over the rolling window power distribution; non-heating and active-heating thresholds are derived from cluster means with hysteresis. If confidence is insufficient (samples, cluster balance, or separation), affected observations are excluded and diagnostics expose the reason.
- **Observation quality:** Max gap 45 minutes and min daily coverage 90%; invalid/incomplete days are excluded and counted diagnostically.
- **Storage:** Power samples for the learning window are kept in `.storage/energyadvisor.water_heater_samples.<entry_id>` and written at most every 10 minutes and on shutdown. The plan, humidity maxima and daily observations are kept in the restore state.
- **Future improvements (not implemented):** solar-production forecasts, weather adjustments, seasonal models, advanced occupancy prediction, and Saturday afternoon replenishment planning.

### `sensor.energy_advisor_water_heater_diagnostics`
- **Description:** Diagnostic-category sensor for the water-heater planner. Its state is `complete` or `incomplete`; the diagnostic attributes update whenever the schedule sensor updates.
- **Default Entity ID:** `sensor.energy_advisor_water_heater_diagnostics` for the first config entry.
- **Attributes:**

| Attribute | Description |
|-----------|-------------|
| `current_recommended_state` | Current recommendation, `on` or `off`. |
| `required_runtime_hours` | Estimated runtime demand used to create the plan. |
| `required_slot_count` | Number of price slots required for the runtime estimate. |
| `selected_runtime_category` | Learning category used, such as `shower` or `no_shower`. |
| `shower_detected_today` | Whether a shower was detected for today's plan. |
| `total_expected_schedule_cost` | Expected electricity cost of the selected schedule. |
| `historical_sample_count` | Number of historical runtime samples used by the estimate. |
| `runtime_percentile` | Runtime percentile used for the demand estimate. |
| `last_calculation_time` | Local time of the latest planning calculation. |
| `runtime_capped` | Whether the requested runtime was capped by the configured maximum. |
| `max_hours_adequacy` | Evidence-based assessment of the configured maximum runtime. |
| `plan_status` | `complete` or `incomplete`. |
| `plan_incomplete_reason` | Reason the plan could not meet runtime demand, if any. |
| `valid_daily_count` | Number of valid days in the learning window. |
| `excluded_daily_count` | Number of invalid or incomplete days excluded from learning. |
| `learning_source_level` | Fallback level used to select historical runtime samples. |
| `heater_state_source` | Source used to determine heater activity: active entity, power-derived, or unknown. |
| `threshold_confidence` | Confidence status for the power-based heating thresholds. |
| `threshold_active_heating_w` / `threshold_non_heating_w` | Derived power thresholds in watts. |
| `threshold_sample_count` | Number of power samples used to derive thresholds. |
| `max_gap_minutes_limit` | Maximum allowed gap between samples for a valid day. |
| `min_daily_coverage_ratio` | Minimum daily sample coverage required for a valid observation. |
| `max_hours_adequacy_evidence` | Valid and exceeding day counts used for the maximum-runtime assessment. |
| `window_start` / `window_end` | Local start and end of the planned 24-hour window. |
| `missing_config` | Required configuration values that are missing. |
| `plan_available` | Whether a schedule plan is available. |
| `updated_at` | Local time when these diagnostic values were read. |

The schedule sensor itself retains only `on_slots`, the selected slots with local
`start` and `end`, expected `cost`, and `duration` in minutes. This table is the
complete attribute reference for both water-heater sensors.

See [docs/waterheater.md](docs/waterheater.md) for how the plan is made and
learned.

The battery options in the configuration (capacity, power limits, SoC bounds,
wear cost and SoC sensor) are described in the [Battery](#battery)
configuration table. Without a battery capacity and a max charge power, or with
a min SoC that is not below the max SoC, the sensor stays on `standby` with a
`target_soc` of `50` in every slot.

#### Planner

The planner optimizes the battery over all known price slots, then increases the
charge margin to protect against underestimated household demand. It uses a
dynamic programme over the battery state of charge, written in plain Python
without a solver or extra package. It runs in an executor job, so it never
blocks Home Assistant.

How far ahead it plans:
- Every price slot of the price sensor that has not ended is planned. There is no
  look-ahead limit and the configuration option
  `battery_optimization_horizon_hours` is not used (see the [Battery](#battery)
  table).
- The price sensor holds today's prices, and tomorrow's as well once they are
  published. Nord Pool does that in the afternoon, usually around 13:30. The
  plan therefore covers the rest of today (about 10 to 24 hours) until tomorrow's
  prices arrive, and the rest of today plus tomorrow (about 24 to 35 hours)
  after that. It follows the number of price slots, whatever it is.
- Energy still in the battery after the last known price slot is worth something
  to the plan: `η × the median cost of the planned slots − the wear cost` per
  stored kWh (`η` is the efficiency of one direction, `√0.9`), but never less
  than 0. That is roughly what the energy saves once new prices arrive, and
  without it the battery would be sold off at the end of the known prices at any
  positive credit. The battery is therefore not necessarily empty at the end of
  the plan. It is used in the slots that cost more than the typical price of the
  slots left and kept in the others. The next plan looks further ahead when new
  prices arrive.

What it works with:
- **Prices:** The `cost` of each price slot (import price with all fees and
  taxes) and its `credit` (the price received for exported energy, with all fees
  and taxes of selling included in the same way). Slots can be 15 or 60 minutes
  long, and a day with a clock change can have 92 or 100 slots.
- **Household load:** The base load from the household Load forecast, matched to
  the price slots by start time (a longer price slot averages the forecast slots
  it covers). Slots without a forecast, or without the household inputs
  configured, use 1.0 kW. The load forecast leaves out the water heater. It runs
  in the day's cheapest hours regardless, and a simulation showed that planning
  for it does not change the savings, so the heater is not modelled.
- **Battery:** Usable window between min and max SoC, max charge and discharge
  power (discharge defaults to the charge power; `sell` uses the discharge
  power, since there is no export limit), and a round-trip efficiency of
  90 % split evenly between charging and discharging. The wear cost
  (`battery_degradation_cost`) is added for every kWh the battery delivers,
  whether it goes to the household or to the grid.
  It is in the same unit as the price sensor's `cost` (see **Prices** above),
  with fees, taxes and VAT included. It does not apply to the raw spot price
  (for example from elprisetjustnu.se), which is much lower.
- **State of charge:** The current value of `battery_soc_entity`, limited to
  the min and max SoC. When it is unknown or unavailable the planner continues
  from the end-of-slot `target_soc` of the previous plan for the slot before the
  current one, or from the min SoC when there is no such plan.

How it decides:
- `charge` buys energy from the grid for the battery, on top of the household
  load. `discharge` covers the household load from the battery and never
  exports. `sell` discharges for more than the household uses, so that the rest
  is exported to the grid, and nothing is imported in the slot. `standby` leaves
  the battery alone. `charge` and `sell` never share a slot.
- The initial plan moves the battery when the price difference pays for the
  losses and wear cost. It then fills more aggressively in selected grid-charge
  periods, so the extra energy may cost more than its forecast savings.
- Selling is part of the same optimisation, with the slot's `credit` as the
  revenue for every exported kWh. The `credit` already includes all taxes and
  fees of selling, so there is no sell margin to set. Only the battery limits
  the export: there is no export limit to the grid, so a `sell` slot can export
  at the max discharge power. Selling retains 30% of the usable capacity
  between configured min and max SoC; household discharge can use that reserve.
  A slot whose `credit` is unknown, `0` or negative never sells.
- A kWh is worth selling when the `credit` is higher than the cost it was
  charged at, adjusted for the round-trip losses, plus the wear cost. With the
  90 % round-trip efficiency that is `credit > cost at charge / 0.9 + wear
  cost`. Energy charged at 1.00 with a wear cost of 0.70 is therefore sold at a
  `credit` above 1.81. In a `sell` slot the battery covers the household load
  first, which saves the slot's `cost`, and exports the rest.
- Once the cost-optimized plan selects grid charging, the planner also uses
  immediately preceding standby slots at the same or a lower price and charges
  at the maximum available power, aiming for the configured max SoC. A short
  window or limited power may prevent a full charge. An ongoing charge period
  continues across replans and restarts unless its price increases. Discharging
  is limited by forecast household use; selling is limited by discharge power
  and the reserve.
- The plan is made from now. The current slot is planned as a whole slot from
  the state of charge now, so its mode stays the same until the slot ends; its
  `target_soc` is the state of charge expected at the end of the slot. Slots
  that have ended keep the mode and `target_soc` they had while they ran. They
  are remembered for two days, also across a restart. A slot that never ran
  since the integration started is `standby`, with the state of charge the plan
  started from as `target_soc`.
- A planned `discharge` in daylight (the middle of the slot lies between
  sunrise and sunset, from Home Assistant's location) is published as `maxuse`
  only when storing an extra solar energy step at the planned end SoC is worth
  at least its export credit, discounted by 20% for forecast uncertainty.
  The storage value comes from the remaining plan, including future prices,
  charging opportunities, losses, wear and the value after the known prices.
  Otherwise it stays `discharge`, so surplus solar can be exported rather than
  stored. This also applies when no surplus is forecast but unexpected solar
  arrives. At night it stays `discharge`. `sell` is never changed to `maxuse`.
- Forecast solar offsets household load and can charge the battery or be
  exported. There is no export limit, so selling is allowed in daylight.

#### When it is calculated

A single worker plans for the sensor. A plan is requested by these triggers:

| Trigger | When it fires |
|---------|---------------|
| Prices | Every time the price sensor has new rates. That is when the configured Nord Pool entity changes state (normally once per price period, so every 15 minutes) and when the Nord Pool coordinator publishes data. The coordinator publishes once a day when tomorrow's prices are fetched. Until then it asks again every 2 minutes from 13:00 and publishes each time, so expect about 15 extra plans before the prices arrive around 13:30. |
| Household load forecast | Twice every quarter hour: at :00, :15, :30 and :45 when the forecast shell is republished, and again when the rebuilt forecast is ready. Only when the household meter is configured. |
| Battery state of charge | Whenever the `battery_soc_entity` differs by 2 percentage points or more from the state of charge the last plan started from. A 5 kW charge of a 15 kWh battery moves it about 33 points per hour, so this fires about every 3.6 minutes while charging at full power, and about every 12 minutes when discharging into a 1.5 kW load. |
| Quarter hour | At :00, :15, :30 and :45, so that the state and the targets roll forward. 96 times per day. |

At every quarter hour the triggers above arrive within a second or so of each
other. Only one plan runs at a time. Triggers that arrive while a plan is being
calculated are merged into one more plan with the latest inputs, and the plan
that was being calculated is discarded because it is outdated. Typically this
means two plans per quarter hour (the first for the quarter-hour trigger, the
second when the rebuilt household forecast arrives), about 190 plans per day
with an idle battery. State of charge changes add to this while the battery is
charging or discharging: three hours of full-power charging and nine hours of
discharging in a day add up to roughly 100 plans. Charging at full power all day
would give about 24 plans per hour. At 15 to 25 ms per plan this is a few seconds
of calculation per day.

#### Battery charge modes

Each slot in `modes` has exactly one of these six modes. They are mutually
exclusive within a slot. Only `maxuse`, `discharge`, `charge`, `sell` and
`standby` are assigned so far, by the [planner](#planner); `pvcharge` is not
used yet because solar is not part of the planning. `maxuse` is used in place of
`discharge` during daylight when solar storage beats export, never in place of
`sell`.

| Mode | Meaning |
|------|---------|
| `maxuse` | Self-consumption. Solar serves the household first and surplus solar charges the battery. If solar is not enough, the battery serves the household, and if that is still not enough, energy is imported from the grid. The battery never charges from the grid and never exports. This is the default fallback when no other mode is clearly better. |
| `charge` | The battery is charged from the grid, possibly alongside solar. Pure solar charging belongs to `maxuse` or `pvcharge`, not `charge`. |
| `discharge` | The battery discharges to serve the household load, which reduces grid import. The battery does not charge from solar in this mode. |
| `pvcharge` | The battery is charged from surplus solar only. Solar serves the household first and any surplus charges the battery; remaining household demand is imported from the grid. The battery never charges from the grid and never discharges. |
| `standby` | The battery is idle: it neither charges nor discharges. Unlike `maxuse` it does not absorb surplus solar or cover household demand. |
| `sell` | The battery discharges to export at a favourable price. It serves the household load first and exports the surplus, and nothing is imported from the grid while exporting. The planner picks it when the slot's `credit` pays for the energy (see [Planner](#planner)). It never charges in the same slot. |

`target_soc` is the battery state of charge, in whole percent (always rounded
up), planned for the end of the slot. It follows from the plan: it rises in `charge` slots, falls in
`discharge`, `sell` and `maxuse` slots and stays the same in `standby` slots. It is a
fixed `50` while the battery is not configured. In a `sell` slot whatever
controls the battery should export until the measured state of charge reaches
`target_soc`, and not below it.

### `energyadvisor.get_levels`
- **Description:** The price levels for today and tomorrow as a string with one char per time period. Main purpose is to provide data for the Level Indicator Clock (https://github.com/Klurige/LevelIndicatorClock)
- **Input parameters:**
 - `entity_id`: Optional `sensor.energy_advisor_price` entity id. Required when multiple Energy Advisor entries are loaded.
 - `level_length`: The length of each level in minutes. Default `0` means the same length as the Nord Pool price periods.
- **Output:**
 - `level_length`: The length of each level in minutes.
 - `levels`: A string representing the price level pattern, where each character corresponds to one `level_length` slot in minutes, starting at midnight today and covering today and tomorrow. Trailing unknown slots are left out. Each character is:
   - `L` for Low
   - `M` for Medium
   - `H` for High
   - `U` for Unknown (if no price is available for that period)
 - `low_threshold`: The threshold cost for low prices.
 - `high_threshold`: The threshold cost for high prices.

The service and the compact sensor use the main sensor's internal rate data directly, so they follow the same threshold decisions as `sensor.energy_advisor_price` without recomputing from rounded state attributes.

### `energyadvisor.debug_select_date`
- **Description:** Only available while the `debug` option is on (see [Debug mode](#debug-mode)). Fetches the prices of one date from elprisetjustnu.se and makes `sensor.energy_advisor_price` and the battery charge mode schedule use them as today's prices.
- **Input parameters:**
 - `date`: The date to show, for example `2026-10-24`. Required.
 - `area`: Optional price area, `SE1` to `SE4`. Defaults to the area found in the Nord Pool prices sensor entity id (for example `sensor.nord_pool_se4_current_price` is `SE4`). It is remembered for later calls.
 - `entity_id`: Optional `sensor.energy_advisor_price` entity id. Required when several entries are in debug mode.
- **Output:** None. The action fails with an error when the area is unknown, the date has no published prices, or elprisetjustnu.se cannot be reached.

## Ranking
The `sensor.energy_advisor_price` sensor provides a `rank` attribute that indicates the current price's rank compared to other prices for the current day. The rank is expressed on a minute scale across a 1440-minute day.
- `0` is the lowest-price slot for the day.
- The highest rank is the start minute of the most expensive slot. For 24 hourly prices this is `1380`, and for 96 quarter-hour prices this is `1425`.

For example, to find the three cheapest hours of the day, look for ranks between 0 and 179. (3 hours × 60 minutes = 180 minutes, so ranks 0–179).
Note that this will find non-consecutive time slots.

#### Notes
- The sensors rely on the NordPool integration for price updates. If the NordPool sensor is delayed or unavailable, these sensors will reflect the latest available data.
- The `rates` attribute in the price sensor provides a forecast of upcoming prices and levels, which can be used for advanced automations or visualizations.

## Visualisation
The data can be visualized using the ApexCharts card.
Here is an example of how the data can be visualized in Home Assistant.
Also needed is the ´config-template-card´.

```yaml
type: custom:config-template-card
variables:
  thresholdLow: states["sensor.energy_advisor_price"].attributes.low_threshold
  thresholdHigh: states["sensor.energy_advisor_price"].attributes.high_threshold
entities:
  - sensor.energy_advisor_price
card:
  type: custom:apexcharts-card
  graph_span: 48h
  span:
    start: day
  experimental:
    color_threshold: true
  series:
    - entity: sensor.energy_advisor_price
      name: Electricity Price
      type: column
      color: green
      float_precision: 2
      extend_to: end
      data_generator: |
        return entity.attributes.rates.map((rate, index) => {
          return [new Date(rate["from"]).getTime(), rate["cost"]];
        });
      color_threshold:
        - value: -1000000
          color: green
        - value: ${vars.thresholdLow}
          color: yellow
        - value: ${vars.thresholdHigh}
          color: red
```

Or if you prefer one graph for today and one for tomorrow (These graphs also sets min and max values for the y-axis):

```yaml
type: custom:config-template-card
variables:
  thresholdLow: states["sensor.energy_advisor_price"].attributes.low_threshold
  thresholdHigh: states["sensor.energy_advisor_price"].attributes.high_threshold
entities:
  - sensor.energy_advisor_price
card:
  type: custom:apexcharts-card
  header:
    title: Prices today
    show: true
    show_states: true
  graph_span: 24h
  span:
    start: day
  now:
    show: true
    label: Nu
  experimental:
    color_threshold: true
  apex_config:
    yaxis:
      min: 0
      max: 7
  series:
    - entity: sensor.energy_advisor_price
      name: Import
      type: column
      color: green
      float_precision: 2
      extend_to: end
      show:
        in_header: before_now
        extremas: true
      data_generator: |
        return entity.attributes.rates.map((rate, index) => {
          return [new Date(rate["from"]).getTime(), rate["cost"]];
        });
      color_threshold:
        - value: -1000000
          color: green
        - value: ${vars.thresholdLow}
          color: yellow
        - value: ${vars.thresholdHigh}
          color: red
```

```yaml
type: custom:config-template-card
variables:
  thresholdLow: states["sensor.energy_advisor_price"].attributes.low_threshold
  thresholdHigh: states["sensor.energy_advisor_price"].attributes.high_threshold
entities:
  - sensor.energy_advisor_price
card:
  type: custom:apexcharts-card
  header:
    title: Prices tomorrow
    show: true
    show_states: true
  graph_span: 24h
  span:
    start: day
    offset: +24h
  experimental:
    color_threshold: true
  apex_config:
    yaxis:
      min: 0
      max: 7
  series:
    - entity: sensor.energy_advisor_price
      name: Import
      type: column
      color: green
      float_precision: 2
      extend_to: end
      data_generator: |
        return entity.attributes.rates.map((rate, index) => {
          return [new Date(rate["from"]).getTime(), rate["cost"]];
        });
      color_threshold:
        - value: -1000000
          color: green
        - value: ${vars.thresholdLow}
          color: yellow
        - value: ${vars.thresholdHigh}
          color: red
      show:
        extremas: true
```

## Troubleshooting
- Ensure the NordPool integration is working and provides price data.
- Restart Home Assistant after installation or configuration changes.
- Check logs for errors related to `energyadvisor`.

### Debug mode
For development only. Turn on `Debug mode` on the first options page (`Settings` -> `Devices & Services` -> `Energy Advisor` -> `Configure`). Saving reloads the integration, and then:

- The prices come from `https://www.elprisetjustnu.se/api/v1/prices/<year>/<MM-DD>_<area>.json` instead of the Nord Pool integration, one date at a time. The Nord Pool sensor is only used to find the area (`SE1` to `SE4`) in its entity id. At start-up today's prices are fetched. The currency is the configured one when it is `SEK` or `EUR`, otherwise `SEK`.
- The `energyadvisor.debug_select_date` action exists. The prices are collected for the chosen date (any date with published prices, past or future), but they are moved to **today** with the same time of day: 19:00 on the chosen date becomes 19:00 today. So everything that only looks at the current time, such as the current price, level and rank of `sensor.energy_advisor_price`, the compact levels and the dashboard graphs, uses them as today's prices. `sensor.energy_advisor_battery_charge_mode` (its `modes` attribute) follows immediately. The chosen date stays active, and when the local day changes at midnight it is moved to the new day.
- The Nord Pool sensor's units and `price_divisor` are ignored. Other sensors may show odd values, but the integration keeps running. Fetch errors are logged or returned by the action; they never stop the integration.

Example button card for a dev dashboard:

```yaml
type: button
name: Show 2026-10-24
tap_action:
  action: perform-action
  perform_action: energyadvisor.debug_select_date
  data:
    date: "2026-10-24"
```

To pick the date from a date helper (`input_datetime` with only the date enabled), call the action from a script and let the button run the script:

```yaml
script:
  energyadvisor_show_date:
    alias: Show the selected date
    sequence:
      - action: energyadvisor.debug_select_date
        data:
          date: "{{ states('input_datetime.energyadvisor_debug_date') }}"
```

### Debug logging
Add this to your `configuration.yaml` and restart Home Assistant to debug the component.

```yaml
logger:
  default: info
  logs:
    custom_components.energyadvisor: debug
    custom_components.energyadvisor.sensor.price: info
    custom_components.energyadvisor.sensor.compactlevels: info
    custom_components.energyadvisor.util: debug
```

## Contributing
Contributions are welcome! Please open issues or pull requests on GitHub.

### Development

#### Unit tests
To run the unit tests from command line, you should first setup a virtual environment and install the required dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.test.txt
```

Then you can run the tests with:

```bash
pytest -v --cov=custom_components.energyadvisor tests/
```

Or without coverage:

```bash
pytest -v tests/
```

To enable the tracked pre-commit hook in this clone, set the repository hook
path once:

```bash
git config --local core.hooksPath .githooks
```

The hook runs the same unit-test and formatting checks used in CI:
`black --check .` and `pytest --maxfail=1 --disable-warnings -q`.
After those pass, it updates and stages the integration manifest version to one
patch above the nearest reachable numeric release tag. For example, commits after
tag `0.0.17` all use `0.0.18` until the next release is tagged. Fetch release tags
with `git fetch --tags` before committing if they are missing from your clone.
If a manifest needs updating but has unstaged edits, stage or stash those edits
first; the hook will not overwrite them.

To publish, push your commits and run **Actions -> Release -> Run workflow** on
the desired branch. No version input is needed: the workflow tags the selected
commit with its manifest version and creates a GitHub release with generated
notes. It does not change the manifest or create a version-bump commit. The next
local commit after fetching that tag starts the next patch version.

## License

This project is licensed under the GNU General Public License (GPL). See the [LICENSE](LICENSE) file for details.

## Say Thanks
If you like this project, please consider supporting me.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/klurige)
