# Energy Advisor for Home Assistant

A custom component for Home Assistant that provides Energy Advisor price sensors based on data from the Home Assistant NordPool integration. This integration helps you monitor and automate your home based on real-time and forecasted electricity prices, using the NordPool sensor as a data source. It also provides optional household load and solar production forecasts, and is being built towards a battery charge schedule.

This integration works particularly well with the [LevelIndicatorClock](https://github.com/Klurige/LevelIndicatorClock), allowing you to visualize price levels throughout the day in an analogue clock.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/klurige)

---

## Features
- Uses electricity prices provided by the Home Assistant NordPool integration.
- Categorizes prices into levels (e.g., low, medium, high).
- Provides two price sensors plus a placeholder battery charge mode sensor, an optional household Load forecast sensor, and an optional refined solar forecast sensor.
- Supports multiple languages (English, Swedish).
- Easy configuration via the Home Assistant UI.
  - Adds support for extra fees and taxes from your grid and supplier.
  - Allows for setting thresholds for low and high prices.
  - Adds support for credits when exporting electricity.
- Provides a ranking system for prices to help identify the best times to use electricity.
- Exposes a household Load forecast that falls back to a cold-start shell and
  learns a seasonal baseline from retained slot history.
- Can refine a solar production forecast using your solar's measured output.
- Provides a placeholder battery schedule (one `standby` slot per price slot); the mode logic is not implemented yet.

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

Inputs marked *currently not in use* are accepted and stored in the config entry, but they do not change any sensor value. They are kept for the battery charge mode logic that is being rebuilt. Where noted, the household forecast also records their history.

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

#### Solar forecast

| Key                        | Description                  | Example Value | Status |
|----------------------------|------------------------------|---------------|--------|
| `forecast_entity`          | Raw solar forecast sensor for today. Set together with `power_entity` | `sensor.home_energy_production_today` | In use |
| `power_entity`             | Solar actual power sensor in watts. Set together with `forecast_entity` | `sensor.solar_active_power` | In use |
| `forecast_tomorrow_entity` | Raw solar forecast sensor for tomorrow; has no effect without the two keys above | `sensor.home_energy_production_tomorrow` | In use |

See [docs/solarforecast.md](docs/solarforecast.md) for details.

#### Battery

Battery capacity and max charge power must be set together. Min SoC must be lower than max SoC. None of the battery options is used yet: the battery charge mode sensor only reads the price schedule.

| Key                        | Description                  | Example Value | Status |
|----------------------------|------------------------------|---------------|--------|
| `battery_capacity_kwh`     | Usable battery capacity; set together with max charge power | `10.0` | currently not in use |
| `battery_max_charge_power_w` | Maximum battery charge power; set together with capacity | `5000` | currently not in use |
| `battery_max_discharge_power_w` | Maximum battery discharge power; the form pre-fills it with the max charge power | `5000` | currently not in use |
| `battery_optimization_horizon_hours` | Look-ahead horizon in hours (1-48, form default 48) | `48` | currently not in use |
| `battery_min_soc_pct`      | Lower SoC bound in % (form default 5) | `5` | currently not in use |
| `battery_max_soc_pct`      | Upper SoC bound in % (form default 95) | `95` | currently not in use |
| `battery_degradation_cost` | Minimum price spread required before the battery is cycled; no default | `0.7` | currently not in use |
| `battery_soc_entity`       | Battery state-of-charge sensor | `sensor.home_battery_soc` | currently not in use (history is recorded but not used) |
| `battery_charge_power_entity` | Battery charge power sensor | `sensor.home_battery_charge_power` | currently not in use (history is recorded but not used) |

#### Grid metering and household inputs

All of these are optional sensors. Only `power_meter_consumption` enables a feature: the household Load forecast sensor.

| Key                        | Description                  | Example Value | Status |
|----------------------------|------------------------------|---------------|--------|
| `grid_import_entity`       | Grid import power sensor | `sensor.grid_import_power` | currently not in use (history is recorded but not used) |
| `grid_export_entity`       | Grid export power sensor | `sensor.grid_export_power` | currently not in use (history is recorded but not used) |
| `power_meter_consumption`  | Whole-house energy meter (cumulative kWh) or power sensor. Creates the Load forecast sensor | `sensor.household_energy_total` | In use |
| `outdoor_temperature_entity` | Outdoor temperature sensor | `sensor.outdoor_temperature` | currently not in use (history is recorded but not used) |
| `water_heater_power_entity` | Water heater power sensor; its measured power is subtracted from the meter | `sensor.water_heater_power` | In use |
| `water_heater_power_w`     | Water heater rated power in W; fallback when the power sensor has no history | `3500` | In use (fallback) |
| `water_heater_active_entity` | Water heater active sensor; used with the rated power as fallback | `binary_sensor.water_heater_active` | In use (fallback) |
| `water_heater_max_hours`   | Worst-case water heater refill time in hours | `4` | currently not in use |
| `bathroom_humidity_entity` | Bathroom humidity sensor | `sensor.bathroom_humidity` | currently not in use (history is recorded but not used) |
| `central_heating_power_entity` | Central heating power sensor; its measured power is subtracted from the meter | `sensor.central_heating_power` | In use |
| `central_heating_power_w`  | Central heating rated power in W; fallback when the power sensor has no history | `2200` | In use (fallback) |
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

## Usage
- The integration adds the price sensor, the compact levels sensor, one battery charge mode sensor, one optional household Load forecast sensor, one optional solar forecast sensor, and one service. The default entity ids for the first config entry are `sensor.energy_advisor_price`, `sensor.energy_advisor_compact_levels`, `sensor.energy_advisor_battery_charge_mode`, `sensor.energy_advisor_load_forecast` when the household meter is configured, and `sensor.energy_advisor_solar_forecast` when both solar forecast inputs are configured. Additional config entries receive numeric suffixes such as `sensor.energy_advisor_price_2`.
  - `sensor.energy_advisor_price` provides the current electricity price with all fees and taxes included, and a list of all known upcoming prices. (Nordpool gets the next day prices around 14:00 CET)
  - `sensor.energy_advisor_compact_levels` provides a compact level string intended for integrations such as Level Indicator Clock.
  - `sensor.energy_advisor_battery_charge_mode` provides a placeholder battery schedule: one `standby` entry per price slot, with the slot's `cost` and `credit` and a fixed `target_soc` of 50 %.
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

### `sensor.energy_advisor_battery_charge_mode`
- **Description:** Placeholder battery schedule built from the linked `sensor.energy_advisor_price` entry. The planner is being rebuilt from scratch; price is its only input and no optimization is done yet.
- **Default Entity ID:** `sensor.energy_advisor_battery_charge_mode` for the first config entry.
- **State:** `standby`.
- **Attributes:**
  - `modes`: One entry per price slot, in the same order and with the same start and end times as the `rates` attribute of `sensor.energy_advisor_price`. Each entry has the local slot start `from`, the slot end `to`, `mode` (one of the [modes](#battery-charge-modes) below; always `standby` for now), `target_soc` (target battery state of charge in percent; fixed at `50` for now), and `cost` and `credit` copied from the same price slot (rounded to 2 decimals).

The battery options in the configuration (capacity, power limits, SoC bounds,
SoC sensor and so on) are kept, but this sensor does not use them yet; see the
[Battery](#battery) configuration table.

#### Battery charge modes

Each slot in `modes` has exactly one of these six modes. They are mutually
exclusive within a slot. The mode logic is not implemented yet: until it is,
every slot is `standby`.

| Mode | Meaning |
|------|---------|
| `maxuse` | Self-consumption. Solar serves the household first and surplus solar charges the battery. If solar is not enough, the battery serves the household, and if that is still not enough, energy is imported from the grid. The battery never charges from the grid and never exports. This is the default fallback when no other mode is clearly better. |
| `charge` | The battery is charged from the grid, possibly alongside solar. Pure solar charging belongs to `maxuse` or `pvcharge`, not `charge`. |
| `discharge` | The battery discharges to serve the household load, which reduces grid import. The battery does not charge from solar in this mode. |
| `pvcharge` | The battery is charged from surplus solar only. Solar serves the household first and any surplus charges the battery; remaining household demand is imported from the grid. The battery never charges from the grid and never discharges. |
| `standby` | The battery is idle: it neither charges nor discharges. Unlike `maxuse` it does not absorb surplus solar or cover household demand. |
| `sell` | The battery discharges to export at a favourable price. It serves the household load first and exports the surplus, and nothing is imported from the grid while exporting. |

`target_soc` is the battery state of charge, in percent, planned for the end of
the slot. How it is set for each mode is not decided yet; it is a fixed `50`
for now.

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


## License

This project is licensed under the GNU General Public License (GPL). See the [LICENSE](LICENSE) file for details.

## Say Thanks
If you like this project, please consider supporting me.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/klurige)
