# Water-Heater Schedule Binary Sensor

## Purpose

`binary_sensor.energy_advisor_waterheater` tells you when to run the water
heater. Once a day it picks the cheapest 15-minute price slots for the next 24
hours and keeps that plan fixed. Its state is `on` during a planned slot and
`off` otherwise. It only recommends; an automation has to switch the heater.

`sensor.energy_advisor_water_heater_diagnostics` shows how the plan was made
(see the attribute table in the [README](../README.md#sensorenergy_advisor_water_heater_diagnostics)).

---

## Input

| Option | Key | Used for |
|---|---|---|
| Water heater power sensor | `water_heater_power_entity` | Learning how long the heater runs each day |
| Bathroom humidity sensor | `bathroom_humidity_entity` | A day where humidity reaches 100 % is a shower day |
| Water heater rated power (W) | `water_heater_power_w` | Expected cost of the plan |
| Worst-case refill hours | `water_heater_max_hours` | The most runtime planned per day |
| Water heater active sensor (optional) | `water_heater_active_entity` | Refreshes the state when the heater switches |

All four required options must be set and both sensors must have a value;
otherwise the sensor is unavailable and `missing_config` lists what is missing.
Prices come from the linked `sensor.energy_advisor_price` (the `cost` of each
slot, at full precision).

---

## The plan

- **Window:** 20:00 to 20:00 the next day, local time.
- **When it is made:** at 20:00. If there is no plan for the current window
  (first start, or Home Assistant was not running at 20:00), a plan is made
  right away for the time that is left. Slots that have already ended are never
  selected.
- **Fixed:** a plan with selected slots is not changed until its window ends,
  so new prices or a restart never make the heater run twice. A plan that
  selected nothing because no prices were known is retried every 5 minutes.
- **Selection:** the required runtime is rounded up to whole 15-minute slots,
  capped at `water_heater_max_hours`, and the cheapest slots in the window are
  chosen. `plan_incomplete_reason` is `runtime_capped` when the cap applied and
  `insufficient_slots` when there were not enough prices.

---

## Learning the runtime

1. **Power samples.** Every change of the power sensor (and a reading every 5
   minutes) is kept for 32 days.
2. **Thresholds.** The power values are split into "heating" and "not heating"
   with a 40-bin histogram and Otsu's method. Without enough samples or a clear
   split, `threshold_confidence` explains why, and no day is learned.
3. **Daily runtime.** For each of the last 30 days the hours spent heating are
   counted. A day is skipped when a gap between samples is longer than 45
   minutes or less than 90 % of the day is covered.
4. **Estimate.** The 95th percentile of the runtimes of matching days is used.
   Days are matched on the first level with at least 10 days: same weekday and
   same shower category, same weekday, other weekdays with the same category,
   all days. With fewer than 10 days the default of 4 hours is used; from 10 to
   20 days the default and the learned value are blended.
5. **Max hours check.** `max_hours_adequacy` becomes `insufficient` when at
   least 3 of 10 or more valid days needed more than `water_heater_max_hours`.

---

## Performance and storage

- Learning (thresholds and 30 daily observations) runs in the executor once per
  plan. Each day only looks at the samples near it, so the work grows linearly
  with the number of samples. Only the slot selection runs on the event loop.
- Samples are kept in time order, so dropping old ones costs nothing per new
  sample.
- Samples are saved in `.storage/energyadvisor.water_heater_samples.<entry_id>`,
  at most every 10 minutes and when Home Assistant stops. Older versions kept
  them in the restore state; they are moved to the store on the first start.
- The plan, the daily humidity maxima and the daily observations are kept in
  the restore state.

## Graph
The following graph shows the water heater schedule and power consumption:

    type: custom:apexcharts-card
    header:
      show: true
      title: Varmvattenberedare idag
    graph_span: 24h
    span:
      start: day
    yaxis:
      - id: percent
        opposite: true
        min: 0
        max: 100
        decimals: 0
      - id: power
        min: 0
        max: 2500
        decimals: 0
        apex_config:
          tickAmount: 10
    series:
        - entity: binary_sensor.energy_advisor_waterheater
          yaxis_id: percent
          type: line
          curve: stepline
          stroke_width: 2
          data_generator: |
            const slots = entity.attributes.on_slots || [];
    
            const startOfDay = new Date();
            startOfDay.setHours(0, 0, 0, 0);
    
            const data = [];
    
            for (let i = 0; i < 96; i++) {
              const ts = startOfDay.getTime() + i * 15 * 60 * 1000;
    
              const on = slots.some(slot =>
                ts >= new Date(slot.start).getTime() &&
                ts < new Date(slot.end).getTime()
              );
    
              data.push([ts, on ? 100 : 0]);
            }
    
            return data;
        - entity: sensor.varmvatten_effekt
          yaxis_id: power
          stroke_width: 2
          float_precision: 0
          transform: return Math.round(x);
        - entity: sensor.badrum_klimat_humidity
          yaxis_id: percent
          type: line
          stroke_width: 2


## Automation
This automation can be used to control the water heater switch. Replace the
entity IDs with those in your installation. It also synchronizes the switch
when Home Assistant starts or automations are reloaded. After enabling it
manually, use **Run actions** once to apply the current schedule immediately.
If the schedule is unknown or unavailable, the switch is turned off.

        alias: Styr varmvattenbrytare från Energy Advisor
        description: ''
        triggers:
            - trigger: state
              entity_id: binary_sensor.energy_advisor_waterheater
            - trigger: homeassistant
              event: start
            - trigger: event
              event_type: automation_reloaded
        conditions: []
        actions:
            - choose:
                - conditions:
                    - condition: state
                      entity_id: binary_sensor.energy_advisor_waterheater
                      state:
                        - 'on'
                  sequence:
                    - action: switch.turn_on
                      target:
                        entity_id: switch.teknik_varmvatten_brytare
              default:
                - action: switch.turn_off
                  target:
                    entity_id: switch.teknik_varmvatten_brytare
        mode: single
