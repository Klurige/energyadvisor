# Feature: Electricity Price Levels Sensor

## Purpose

Calculates the total electricity purchase cost and sell credit per kWh, classifies
the current price as **Low / Medium / High**, and ranks prices across each day.
Produces two sensor entities consumed by automations, dashboards, and the
[LevelIndicatorClock](https://github.com/Klurige/LevelIndicatorClock).

---

## Input

### Required: Nordpool prices sensor

A [Home Assistant Nordpool integration](https://www.home-assistant.io/integrations/nordpool/)
entity, e.g. `sensor.nord_pool_se4_current_price`.

The coordinator calls the `nordpool.get_prices_for_date` service to fetch raw price
slots. The sensor also subscribes to state-change events on the Nordpool entity so
that it refreshes at the exact moment new data arrives (typically ~14:00 CET for
next-day prices).

Raw prices are returned by the `get_prices_for_date` service always in
**currency/MWh**, regardless of what the Nordpool sensor's `unit_of_measurement`
attribute shows. That attribute (e.g. `SEK/kWh`, `EUR/MWh`) is the HA *display*
unit, which HA auto-converts from the raw MWh service value. The integration
always divides by 1000 to normalise to currency/kWh, since all user-configured
fees are per kWh. If `prices_in_cents` is `true` in the Nordpool sensor attributes,
an additional ÷100 is applied (handled by `price_divisor`).

### Configuration parameters (all stored in `entry.options`)

| Key | Type | Description |
|-----|------|-------------|
| `nordpool_prices_sensor` | entity_id | Nordpool sensor to read prices from |
| `low_threshold` | float | Max cost for "Low" level |
| `high_threshold` | float | Min cost for "High" level |
| `supplier_fixed_fee` | float | Fixed fee from electricity supplier (currency/kWh) |
| `supplier_variable_fee` | float | Variable fee from supplier (%) |
| `supplier_fixed_credit` | float | Fixed credit when selling (currency/kWh) |
| `supplier_variable_credit` | float | Variable credit when selling (%) |
| `grid_fixed_fee` | float | Fixed grid fee (currency/kWh) |
| `grid_variable_fee` | float | Variable grid fee (%) |
| `grid_fixed_credit` | float | Fixed grid credit when selling (currency/kWh) |
| `grid_variable_credit` | float | Variable grid credit when selling (%) |
| `grid_energy_tax` | float | Energy tax added by the grid (currency/kWh) |
| `electricity_vat` | float | VAT on electricity (%) |
| `exclude_from_recording` | bool | Exclude sensors from HA recorder (default: true) |

---

## Price calculation

```
cost_before_vat = spot * (1 + supplier_variable_fee% + grid_variable_fee%)
                + supplier_fixed_fee
                + grid_fixed_fee
                + grid_energy_tax

cost = cost_before_vat * (1 + electricity_vat%)

credit = spot * (1 + supplier_variable_credit% + grid_variable_credit%)
       + supplier_fixed_credit
       + grid_fixed_credit
```

### Level classification

```
cost < low_threshold  → "Low"
cost > high_threshold → "High"
otherwise             → "Medium"
```

Level is `"Unknown"` until the first rate is loaded (startup / no matching slot).

### Ranking

Each price slot is ranked within its calendar day (00:00–23:59 local).
Slot duration follows whatever Nordpool provides — the code makes no assumption.

The day's slots are sorted cheapest-first; the cheapest slot gets rank **0**.
Intermediate slots receive proportional values:
`rank = rank_index × (1440 / slots_per_day)`.

The scale represents minutes in a day. For 24 hourly prices, ranks are
`0, 60, 120, ..., 1380`. For 96 quarter-hour prices, ranks are
`0, 15, 30, ..., 1425`. It can be used to find the cheapest time periods.
For example, running the water heater the four cheapest hours of the day
should turn it on if rank is below 240 and off if rank is 240 or higher.

---

## Output

### `sensor.electricitypricelevels`

| Property | Value |
|----------|-------|
| State | Current cost (float, 5 d.p., e.g. `1.87845`) in `{currency}/kWh` |
| Device class | `monetary` |
| Icon | `mdi:arrow-expand-down` (Low) / `mdi:arrow-expand-vertical` (Medium) / `mdi:arrow-expand-up` (High) |

**Attributes:**

| Attribute | Description |
|-----------|-------------|
| `spot_price` | Raw Nordpool price before fees/taxes |
| `cost` | Total purchase cost including all fees and VAT |
| `credit` | Total credit received when exporting |
| `unit` | Energy unit, e.g. `kWh` |
| `currency` | Currency, e.g. `SEK` |
| `level` | `Low`, `Medium`, `High`, or `Unknown` |
| `rank` | Minute-scaled rank within the day (0 = cheapest) |
| `low_threshold` | Configured low threshold |
| `high_threshold` | Configured high threshold |
| `rates` | Compact list of all known upcoming slots (see format below) |

#### `rates` compact format

```json
[
  { "from": "2026-06-10T00:00", "cost": 1.234, "credit": 0.456, "level": "L", "rank": 480 },
  { "from": "2026-06-10T01:00", "cost": 1.567, "credit": 0.789, "level": "M", "rank": 720 },
  ...
]
```

- `from` — slot start in **local time**, no timezone suffix, ISO 8601
- `cost` / `credit` — 3 decimal places
- `level` — single character: `L` (Low), `M` (Medium), `H` (High), `?` (unknown)
- `rank` — minute-scaled slot rank
- Old rates (before today) are purged each time the sensor state is updated
- Tomorrow's rates appear when Nordpool publishes them (~14:00 CET)

---

### `sensor.compactlevels`

Hidden diagnostic sensor (disabled in entity registry by default).
Consumed by LevelIndicatorClock and the `electricitypricelevels.get_levels` service.

| Property | Value |
|----------|-------|
| State | Integer — minutes since midnight |
| Attribute `compact` | `{minutes}:{level_length}:{past_1h_levels}:{future_12h_levels}` |

Reads the main sensor's internal rate data directly to build level strings.
Updates automatically at the end of each price slot. Level characters: `L`, `M`, `H`, `U` (unknown).

---

### Service: `electricitypricelevels.get_levels`

Returns a dict with level string and slot length, used by LevelIndicatorClock.
When multiple Electricity Price Levels entries are loaded, pass the desired
`entity_id`.

---

## Architecture & data flow

```
NordpoolDataCoordinator  ←──  nordpool.get_prices_for_date (HA service call)
        │
        │  async_update_data(raw_prices)
        ▼
ElectricityPriceLevelsSensor
  • parse entries: spot price → cost/credit via calculate_cost_and_credit()
  • classify: calculate_level(cost)
  • rank: sort each day's entries by price, assign minute-scaled rank
  • store in self._rates (internal dicts with datetime objects)
  • expose via _format_rates_compact() → attributes.rates (serialisable)
        │
        │  direct in-memory callback when rate data changes
        ▼
CompactLevelsSensor
  • reads the main sensor's internal rates, builds level string
  • schedules periodic update at slot boundaries
```

**Key files:**

| File | Role |
|------|------|
| `sensor/__init__.py` | Wires up coordinator + sensors, registers entities |
| `sensor/electricitypricelevels.py` | Core sensor: price calc, level, rank, rates |
| `sensor/compactlevels.py` | Compact level string sensor |
| `sensor/nordpool_coordinator.py` | Calls Nordpool service, feeds data to sensor |
| `coordinator.py` | (Does not exist — see `sensor/__init__.py`) |
| `config_flow.py` | Multi-step UI setup wizard |
| `const.py` | All `CONF_*` constants and dev-config import |

---

## Development setup

### `dev_config.py` (gitignored)

Copy `dev_config.py.example` → `dev_config.py` (or create manually) to pre-fill
the config-flow wizard with real values during development. When `DEV_DEFAULTS_ENABLED = True`,
`config_flow.py` pre-populates every form field from `DEV_DEFAULTS`.

```python
# dev_config.py — never commit
DEV_DEFAULTS_ENABLED = True
DEV_DEFAULTS = {
    "nordpool_prices_sensor": "sensor.nord_pool_se4_current_price",
    "low_threshold": 2.0,
    "high_threshold": 3.0,
    # ... all other CONF_* keys
}
HA_URL   = "http://<host>:8123"
HA_TOKEN = "<long_lived_access_token>"
```

`const.py` imports `DEV_DEFAULTS`, `DEV_DEFAULTS_ENABLED`, `HA_URL`, `HA_TOKEN` with
a `try/except ImportError` fallback so the integration works normally when the file
is absent.

### `scripts/fetch_ha_history.py`

Fetches historical sensor data from a live HA instance over the REST API.
Used to seed inverter/price history for solar forecast calibration.
Requires `HA_URL` and `HA_TOKEN` in `dev_config.py`.

### Tests

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/tmp/mypycache \
  .venv/bin/python -m pytest tests/ -q
```

All tests live in `tests/`. The compact levels sensor has dedicated tests in
`tests/test_compact_levels_sensor.py`. Pass the new compact `rates` format
(`{ "from": ..., "cost": ..., "level": "L/M/H" }`) when mocking state attributes.

---

## Known gotchas

- **`level == "Unknown"` is intentional**, not a bug. It occurs at startup and
  briefly at slot boundaries when no rate matches the current time. Any consumer
  that gates on `level` must treat `"Unknown"` as permissive, not as a blocking state.
- **Old rates are purged lazily** — only when `_update_sensor_state_from_current_rate()`
  runs. Rates from previous days may be present in `self._rates` between state updates.
- **Config entry version migration**: v1 → v2 renames `nordpool_area_id` (bare area
  code like `se3`) → `nordpool_prices_sensor` (full entity id). See `__init__.py →
  async_migrate_entry`.
- **`price_divisor`**: reset to `1` during migration; was erroneously stored as `100`
  in v1 entries and would have inflated all prices 100× if applied.
