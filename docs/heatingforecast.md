# Heating forecast

`sensor.energy_advisor_heating_forecast` forecasts the electric power used for
space heating in 15-minute slots for today and tomorrow (192 slots). It learns
how the heating power depends on outdoor temperature and wind from the recorder
long-term statistics, and applies the learned model to the weather forecast.

The sensor is created when both the central heating power sensor
(`central_heating_power_entity`) and the weather entity (`weather_entity`) are
configured. It does not yet feed the battery planner or the household Load
forecast.

## Inputs

| Option (label) | Key | Step | Required | Use |
|---|---|---|---|---|
| Central heating power sensor | `central_heating_power_entity` | Household activity filters | Yes | Training target. A power sensor (W/kW, `state_class: measurement`) uses the hourly mean; an energy sensor (Wh/kWh, `total_increasing`) uses the hourly change |
| Weather entity | `weather_entity` | Household load | Yes | Temperature and wind forecast (`weather.get_forecasts`, hourly with daily fallback); also a fallback for missing sensor history |
| Outdoor temperature sensor | `outdoor_temperature_entity` | Household load | No | Training temperature (hourly mean). Recommended: a sensor at the house is more accurate than the weather service |
| Wind speed sensor | `wind_speed_entity` | Household load | No | Training wind speed (hourly mean, any HA speed unit) |
| Central heating rated power | `central_heating_power_w` | Household activity filters | No | Cold-start fallback only |

The sensors need long-term statistics, so they must have a `state_class`.
Missing or unusable statistics are reported in `quality_warnings`.

When no temperature or wind sensor is configured, or its history has gaps, the
integration uses the weather entity's current values. These are captured every
10 minutes and stored for 365 days.

## Model

The model is a change-point regression on hourly data:

```
P = d + (a + b·wind) · max(0, Tb − Teff) + c · max(0, Tk − Teff)
```

- `Teff` is the outdoor temperature smoothed with an exponential moving average
  of time constant `tau`, because the building responds slowly.
- `Tb` is the balance temperature, below which heating starts.
- `a` is the heat-loss slope in kW/K.
- `b` is the wind term. Wind raises infiltration and convective losses, so it
  scales the temperature slope: wind matters more the colder it is.
- `Tk` and `c` model the direct-electric backup. Below `Tk` the heat pump
  is supplemented by electric heaters, so the slope becomes steeper. The term
  is used only if it improves the fit.
- `d` is a constant base (for example circulation pumps).

The result is multiplied by an hour-of-day profile (workdays and weekends,
24 values each). The profile is shrunk toward 1 and clamped to 0.5–2.

### Training

- Data: up to 365 days of hourly statistics. Recent hours are weighted more
  (half-life 120 days).
- `tau`, `Tb` and `Tk` are found by grid search. The coefficients are fitted by
  non-negative weighted least squares.
- The model is retrained every 6 hours.
- At least 168 hours of data, 48 of them with heating, are required. Until then
  the sensor uses the fallback below.
- Wind is used only if at least 25 % of the training hours have a wind value.

### Weather calibration

The forecast comes from the weather service, but the model may have been trained
on your own sensors. Over the last 14 days, the integration compares the sensors
with the captured weather-entity values and computes:

- a temperature offset, sensor − weather, clamped to ±5 °C;
- a wind scale, sensor / weather, clamped to 0.1–3.

Both are applied to the forecast. At least 24 paired hours are needed; otherwise
no correction is applied.

### Forecast and correction

- Past slots of today are predicted from the observed weather (hindcast).
  Future slots use the calibrated weather forecast. With a daily forecast only,
  each day uses the mean of `temperature` and `templow`.
- The mean error over the last 3 complete hours is added to the
  coming hours. It is clamped to ±3 kW and decays with a time constant of 6 hours.
- The forecast is recomputed every 15 minutes. The weather forecast is fetched
  every 30 minutes.

### Cold start

Without a trained model, the forecast is a straight line from 0 kW at 15 °C to
the rated heating power at −15 °C (smoothed with `tau` = 12 h). Without a rated
power, the forecast is zero.

## Sensor

- **State:** Forecast heating power in `kW` for the current 15-minute slot.
- **Attributes:**
  - `forecasts`: 192 entries with `from` (local slot start, `YYYY-MM-DDTHH:MM`)
    and `load` (kW). Not recorded.
  - `energy_today_kwh`, `energy_tomorrow_kwh`: forecast energy per day.
  - `quality_status`: `ok`, `degraded` (no hourly weather forecast) or
    `fallback` (no trained model).
  - `quality_warnings`: `heating_statistics_missing`,
    `temperature_statistics_missing`, `wind_statistics_missing`,
    `insufficient_history`, `weather_forecast_missing`,
    `weather_forecast_daily_only`, `recorder_unavailable`.
  - `reason`: human-readable status.
  - `model`: the fitted parameters (`balance_temp_c`, `backup_temp_c`,
    `time_constant_h`, `base_kw`, `slope_kw_per_k`, `wind_kw_per_k_ms`,
    `backup_kw_per_k`, `uses_wind`, `training_hours`, `mae_kw`, `r2`, profile).
    Not recorded.
  - `last_trained`, `last_forecast_generation`, `forecast_source`,
    `temperature_offset_c`, `wind_scale`, `residual_correction_kw`.

## Storage

The model, the calibration and the captured weather hours are stored in the
Home Assistant store `energyadvisor_heating_forecast_<entry_id>`. The training
data itself is read from the recorder and not copied.

## Limitations

- Solar gains, indoor temperature and setpoint changes are not modelled.
- Without a heating power sensor (only an active sensor and rated power), no
  model is trained.
