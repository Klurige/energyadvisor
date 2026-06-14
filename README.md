# Electricity Price Levels for Home Assistant

A custom component for Home Assistant that provides electricity price level sensors based on data from the Home Assistant NordPool integration. This integration helps you monitor and automate your home based on real-time and forecasted electricity prices, using the NordPool sensor as a data source.

This integration works particularly well with the [LevelIndicatorClock](https://github.com/Klurige/LevelIndicatorClock), allowing you to visualize price levels throughout the day in an analogue clock.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/klurige)

---

## Features
- Uses electricity prices provided by the Home Assistant NordPool integration.
- Categorizes prices into levels (e.g., low, medium, high).
- Provides two sensors for use in automations and dashboards.
- Supports multiple languages (English, Swedish).
- Easy configuration via the Home Assistant UI.
  - Adds support for extra fees and taxes from your grid and supplier.
  - Allows for setting thresholds for low and high prices.
  - Adds support for credits when exporting electricity.
- Provides a ranking system for prices to help identify the best times to use electricity.
## Prerequisites
- Home Assistant (2025.0 or newer recommended)
- [NordPool integration](https://www.home-assistant.io/integrations/nordpool/) installed and configured in Home Assistant. This integration supplies the electricity prices that this component depends on.

## Installation

### Option 1: HACS (Recommended)

1. Open Home Assistant and go to HACS.
2. Search for `electricitypricelevels` in Integrations.
3. Install the integration.
4. Restart Home Assistant.

Or use the direct link:
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=custom-components&repository=electricitypricelevels&category=integration)

#### Add the integration via the Home Assistant UI
1. Go to `Settings` -> `Devices & Services`
2. Select `+ Add Integration`
3. Search for `electricitypricelevels` and select it
4. Fill in the name of your NordPool sensor and press `Submit`

### Option 2: Manual
1. Copy the `custom_components/electricitypricelevels` directory into your Home Assistant `custom_components` folder.
2. Restart Home Assistant.
3. Add the integration via the Home Assistant UI.

## Configuration
This integration is configured through the Home Assistant UI. During setup you select the Nord Pool prices sensor to use, then configure optional fees, credits, taxes, thresholds, and recorder behavior.

There are many extra fees and taxes that can be added to the price. Suppliers and grid owners can have many different types of fees, which may vary by region or contract. You can specify the details of these fees in the `supplier_note` and `grid_note` fields, and add up the actual amounts in the corresponding fee fields. This allows you to document and sum up all relevant charges for your specific situation in the configuration.

I have added to the configuration taxes and fees added by my grid and supplier. They are no doubt
called differently for other grids and suppliers.

| Key                        | Description                  | Example Value |
|----------------------------|------------------------------|---------------|
| `nordpool_prices_sensor`   | Nord Pool prices sensor entity id | `sensor.nordpool_kwh_se3_sek_3_10_025` |
| `low_threshold`            | Low price threshold          | 0.8           |
| `high_threshold`           | High price threshold         | 1.5           |
| `supplier_note`            | Supplier note                | "My supplier fees" |
| `supplier_fixed_fee`       | Supplier fixed fee           | 0.12          |
| `supplier_variable_fee`    | Supplier variable fee        | 0.05          |
| `supplier_fixed_credit`    | Supplier fixed credit        | 0.00          |
| `supplier_variable_credit` | Supplier variable credit     | 0.00          |
| `grid_note`                | Grid note                    | "My grid fees" |
| `grid_fixed_fee`           | Grid fixed fee               | 0.20          |
| `grid_variable_fee`        | Grid variable fee            | 0.10          |
| `grid_fixed_credit`        | Grid fixed credit            | 0.00          |
| `grid_variable_credit`     | Grid variable credit         | 0.00          |
| `grid_energy_tax`          | Grid energy tax              | 0.45          |
| `electricity_vat`          | Electricity VAT              | 0.25          |
| `exclude_from_recording`   | Exclude integration sensors from recorder/history | `true` |

When `exclude_from_recording` is `true` (default), Home Assistant recorder/history excludes:
- `sensor.electricitypricelevels`
- `sensor.compactlevels`

In addition, the `rates` attribute on `sensor.electricitypricelevels` is excluded from recorder attribute storage to avoid oversized state attributes.

## Usage
- The integration adds two sensors and one service. The default entity ids for the first config entry are `sensor.electricitypricelevels` and `sensor.compactlevels`. Additional config entries receive numeric suffixes such as `sensor.electricitypricelevels_2`.
  - `sensor.electricitypricelevels` provides the current electricity price with all fees and taxes included, and a list of all known upcoming prices. (Nordpool gets the next day prices around 14:00 CET)
  - `sensor.compactlevels` provides a compact level string intended for integrations such as Level Indicator Clock.
  - `electricitypricelevels.get_levels` provides a string containing one character for each price level. (Level clock pattern. See https://github.com/Klurige/LevelIndicatorClock)
- Use these sensors in automations to optimize energy usage (e.g., run appliances when prices are low).

### `sensor.electricitypricelevels`
- **Description:** The current electricity price, including all configured fees and taxes.
- **State:** The numeric value of the current price.
- **Attributes:**
  - `spot_price`: The raw price from the NordPool sensor (before fees/taxes).
  - `cost`: The total cost including all fees and taxes.
  - `credit`: The total credit received when exporting electricity.
  - `unit`: The energy unit from the selected Nord Pool prices sensor, for example `kWh` or `MWh`.
  - `currency`: The currency of the price, for example `SEK` or `EUR`. Sourced from the NordPool sensor.
  - `level`: Current price level as a string (`Low`, `Medium`, `High`).
  - `rank`: The current rank of the price compared to other prices for the current day. See the [Ranking](#ranking) section for details.
  - `low_threshold`: The threshold cost for low prices.
  - `high_threshold`: The threshold cost for high prices.
  - `rates`: A compact list of today's (and possibly tomorrow's) prices, each with:
    - `from`: The start time of the price period in local time (`YYYY-MM-DDTHH:MM`).
    - `cost`: The total cost for the period, including all fees and taxes.
    - `credit`: The total credit for the period, if applicable.
    - `level`: The price level for the period as `L`, `M`, `H`, or `U`.
    - `rank`: The rank of the price for the period compared to other prices for the current day.
- **Update Frequency:** Updated when new Nord Pool data is processed and when the selected Nord Pool source sensor changes state.

### `sensor.compactlevels`
The integration also provides `sensor.compactlevels`, which exposes electricity price levels in a compact format. It is intended for the Level Indicator Clock (https://github.com/Klurige/LevelIndicatorClock) and similar ESPHome-based clocks.
- **Default visibility:** Disabled by default in the entity registry.
- **Entity ID:** `sensor.compactlevels`
- **State:** Minutes since midnight.
- **Attributes:**
  - `compact`: String containing minutes_since_midnight:level_length:history:future where level_length is in minutes and history and future are two char arrays with one char per level, where:
    * `L` for Low
    * `M` for Medium
    * `H` for High
   * `U` for Unknown (if no price is available for that period)

   Typically, there is one hour of data for history and twelve hours for future, but that is not guaranteed.

### `electricitypricelevels.get_levels`
- **Description:** The price levels for today and tomorrow as a string with one char per time period. Main purpose is to provide data for the Level Indicator Clock (https://github.com/Klurige/LevelIndicatorClock)
- **Input parameters:**
 - `entity_id`: Optional `sensor.electricitypricelevels` entity id. Required when multiple Electricity Price Levels entries are loaded.
 - `level_length`: The length of each level in minutes. Default `0` means the same length as the Nord Pool price periods.
- **Output:**
 - `level_length`: The length of each level in minutes.
 - `levels`: A string representing the current price level pattern, where each character corresponds to a price level for today and tomorrow. Each character represents a `level_length` slot in minutes, with:
   - `L` for Low
   - `M` for Medium
   - `H` for High
   - `U` for Unknown (if no price is available for that period)
 - `low_threshold`: The threshold cost for low prices.
 - `high_threshold`: The threshold cost for high prices.

The service and the compact sensor use the main sensor's internal rate data directly, so they follow the same threshold decisions as `sensor.electricitypricelevels` without recomputing from rounded state attributes.

## Ranking
The `sensor.electricitypricelevels` sensor provides a `rank` attribute that indicates the current price's rank compared to other prices for the current day. The rank is expressed on a minute scale across a 1440-minute day.
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
  thresholdLow: states["sensor.electricitypricelevels"].attributes.low_threshold
  thresholdHigh: states["sensor.electricitypricelevels"].attributes.high_threshold
entities:
  - sensor.electricitypricelevels
card:
  type: custom:apexcharts-card
  graph_span: 48h
  span:
    start: day
  experimental:
    color_threshold: true
  series:
    - entity: sensor.electricitypricelevels
      name: Electricity Price
      type: column
      color: green
      float_precision: 2
      extend_to: end
      data_generator: |
        return entity.attributes.rates.map((rate, index) => {
          return [new Date(rate["start"]).getTime(), rate["cost"]];
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
  thresholdLow: states["sensor.electricitypricelevels"].attributes.low_threshold
  thresholdHigh: states["sensor.electricitypricelevels"].attributes.high_threshold
entities:
  - sensor.electricitypricelevels
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
    - entity: sensor.electricitypricelevels
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
          return [new Date(rate["start"]).getTime(), rate["cost"]];
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
  thresholdLow: states["sensor.electricitypricelevels"].attributes.low_threshold
  thresholdHigh: states["sensor.electricitypricelevels"].attributes.high_threshold
entities:
  - sensor.electricitypricelevels
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
    - entity: sensor.electricitypricelevels
      name: Import
      type: column
      color: green
      float_precision: 2
      extend_to: end
      data_generator: |
        return entity.attributes.rates.map((rate, index) => {
          return [new Date(rate["start"]).getTime(), rate["cost"]];
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
- Check logs for errors related to `electricitypricelevels`.

### Debug logging
Add this to your `configuration.yaml` and restart Home Assistant to debug the component.

```yaml
logger:
  default: info
  logs:
    custom_components.electricitypricelevels: debug
    custom_components.electricitypricelevels.sensor.electricitypricelevels: info
    custom_components.electricitypricelevels.sensor.compactlevels: info
    custom_components.electricitypricelevels.util: debug
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
pytest -v --cov=custom_components.electricitypricelevels tests/
```

Or without coverage:

```bash
pytest -v tests/
```


## License

This project is licensed under the GNU General Public License (GPL). See the [LICENSE](LICENSE) file for details.

## Say Thanks
If you like this project, please consider supporting me.

[![Buy Me A Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/klurige)
