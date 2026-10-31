"""Water-heater schedule planner sensor.

Power samples are kept sorted by time in a deque, so old samples are dropped
from the front in constant time per sample. They are saved in their own
``Store`` file rather than in the restore state, because 30 days of samples can
be large. Learning from the samples runs in the executor; only the cheap slot
selection runs on the event loop.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import math
from typing import TYPE_CHECKING
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_BATHROOM_HUMIDITY_ENTITY,
    CONF_EXCLUDE_FROM_RECORDING,
    CONF_WATER_HEATER_ACTIVE_ENTITY,
    CONF_WATER_HEATER_MAX_HOURS,
    CONF_WATER_HEATER_POWER_ENTITY,
    CONF_WATER_HEATER_POWER_W,
    DOMAIN,
    PREFERRED_SENSOR_ENTITY_IDS,
    build_sensor_unique_id,
)
from ..water_heater_planner import (
    PLANNING_HOUR,
    ROLLING_WINDOW_DAYS,
    DailyObservation,
    PlannedSlot,
    SchedulePlan,
    ThresholdResult,
    build_daily_observations,
    build_schedule_plan,
    compute_max_hours_adequacy,
    derive_thresholds,
    estimate_runtime,
    plan_state_at,
)

if TYPE_CHECKING:
    from .price import PriceSensor

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
RESTORE_KEY = "water_heater_schedule"
# Only read to migrate samples saved in the restore state by older versions.
POWER_SAMPLES_KEY = "power_samples"
HUMIDITY_DAILY_MAX_KEY = "humidity_daily_max"
OBSERVATIONS_KEY = "observations"
PLAN_KEY = "plan"
SAMPLES_STORE_VERSION = 1
SAMPLES_SAVE_DELAY_S = 600
SHOWER_HUMIDITY_PCT = 100.0
SAMPLE_RETENTION = timedelta(days=ROLLING_WINDOW_DAYS + 2)


def _learn_from_samples(
    samples: list[tuple[datetime, float]],
    today: date,
    tz_name: str,
    shower_days: set[date],
) -> tuple[ThresholdResult, list[DailyObservation]]:
    """Derive thresholds and daily observations; runs in the executor."""
    thresholds = derive_thresholds([value for _ts, value in samples])
    observations = build_daily_observations(
        today=today,
        tz_name=tz_name,
        power_samples=samples,
        shower_days=shower_days,
        thresholds=thresholds,
    )
    return thresholds, observations


@dataclass(frozen=True)
class _StoredData(ExtraStoredData):
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return self.payload


class WaterHeaterScheduleSensor(RestoreEntity, SensorEntity):
    """Publishes a fixed daily water-heater ON/OFF recommendation plan."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"on_slots"})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        price_sensor: PriceSensor,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._price_sensor = price_sensor
        self._attr_device_info = device_info
        self._attr_exclude_from_recording = entry.options.get(
            CONF_EXCLUDE_FROM_RECORDING, True
        )

        description = SensorEntityDescription(
            key="waterheaterschedule",
            translation_key="waterheaterschedule",
            icon="mdi:water-boiler",
        )
        self.entity_description = description
        self.entity_id = PREFERRED_SENSOR_ENTITY_IDS[description.key]
        self._attr_suggested_object_id = description.key
        self._attr_unique_id = build_sensor_unique_id(entry, description.key)

        self._plan: SchedulePlan | None = None
        self._observations: list[DailyObservation] = []
        self._thresholds: ThresholdResult = derive_thresholds([])
        self._required_runtime_hours: float = 0.0
        self._required_slots: int = 0
        self._runtime_source_level: str = "default"
        self._runtime_percentile: float | None = None
        self._historical_samples: int = 0
        self._runtime_capped = False
        self._max_hours_adequacy = "unknown"
        self._max_hours_evidence: dict[str, int] = {"valid_days": 0, "exceed_days": 0}
        self._plan_complete = False
        self._plan_incomplete_reason: str | None = None
        self._state = "off"
        self._last_calculation: datetime | None = None

        self._power_samples: deque[tuple[datetime, float]] = deque()
        self._samples_store: Store[dict[str, list[float]]] | None = None
        self._plan_lock = asyncio.Lock()
        self._removed = False
        self._humidity_daily_max: dict[str, float] = {}
        self._boundary_unsubs: list[Callable[[], None]] = []
        self._update_listeners: list[Callable[[], None]] = []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._samples_store = Store(
            self.hass,
            SAMPLES_STORE_VERSION,
            f"{DOMAIN}.water_heater_samples.{self._entry.entry_id}",
        )
        await self._async_restore()

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                self._handle_planning_time,
                hour=PLANNING_HOUR,
                minute=0,
                second=0,
            )
        )

        # Significant clock jumps/startup/restore are covered by periodic refresh.
        self.async_on_remove(
            async_track_time_change(
                self.hass,
                self._handle_periodic_refresh,
                minute=[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55],
                second=0,
            )
        )

        power_entity = self._entry.options.get(CONF_WATER_HEATER_POWER_ENTITY)
        if power_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [power_entity],
                    self._handle_power_state_change,
                )
            )
            self._capture_current_power_sample()

        humidity_entity = self._entry.options.get(CONF_BATHROOM_HUMIDITY_ENTITY)
        if humidity_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [humidity_entity],
                    self._handle_humidity_state_change,
                )
            )
            self._capture_current_humidity_state()

        transition_entity = self._transition_entity_id
        if transition_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [transition_entity],
                    self._handle_transition_state_change,
                )
            )

        self._reschedule_slot_boundaries()
        self._refresh_state_only()
        self._request_plan_if_needed()

    async def async_will_remove_from_hass(self) -> None:
        self._removed = True
        for unsubscribe in self._boundary_unsubs:
            unsubscribe()
        self._boundary_unsubs.clear()
        if self._samples_store is not None:
            await self._samples_store.async_save(self._samples_store_data())
        await super().async_will_remove_from_hass()

    def _samples_store_data(self) -> dict[str, list[float]]:
        return {
            "t": [round(ts.timestamp(), 1) for ts, _value in self._power_samples],
            "v": [value for _ts, value in self._power_samples],
        }

    def _plan_needs_refresh(self, now: datetime) -> bool:
        """Return whether there is no usable plan for the current window.

        A plan whose window has ended is replaced (for example when Home
        Assistant was down at the planning hour). A plan that selected nothing
        because no prices were known yet is retried, since nothing of it has
        run. A plan with selected slots stays fixed until its window ends.
        """
        plan = self._plan
        if plan is None or now >= plan.window_end:
            return True
        return (
            not plan.selected_slots and plan.incomplete_reason == "insufficient_slots"
        )

    @callback
    def _request_plan_if_needed(self) -> None:
        if (
            self._is_config_complete
            and not self._plan_lock.locked()
            and self._plan_needs_refresh(dt_util.now())
        ):
            self.hass.async_create_task(self._async_generate_plan())

    @property
    def _transition_entity_id(self) -> str | None:
        active_entity = self._entry.options.get(CONF_WATER_HEATER_ACTIVE_ENTITY)
        if active_entity and self.hass.states.get(active_entity) is not None:
            return active_entity
        power_entity = self._entry.options.get(CONF_WATER_HEATER_POWER_ENTITY)
        if power_entity and self.hass.states.get(power_entity) is not None:
            return power_entity
        return None

    @property
    def available(self) -> bool:
        return not self._missing_requirements()

    @property
    def state(self) -> str:
        return self._state

    @property
    def extra_restore_state_data(self) -> ExtraStoredData:
        plan_payload = None
        if self._plan is not None:
            plan_payload = {
                "window_start": self._plan.window_start.timestamp(),
                "window_end": self._plan.window_end.timestamp(),
                "category": self._plan.category,
                "shower_detected_today": self._plan.shower_detected_today,
                "required_runtime_hours": self._plan.required_runtime_hours,
                "required_slots": self._plan.required_slots,
                "expected_cost": self._plan.expected_cost,
                "runtime_capped": self._plan.runtime_capped,
                "complete": self._plan.complete,
                "incomplete_reason": self._plan.incomplete_reason,
                "selected_slots": [
                    {
                        "start": slot.start.timestamp(),
                        "end": slot.end.timestamp(),
                        "cost": slot.cost,
                        "duration_minutes": slot.duration_minutes,
                    }
                    for slot in self._plan.selected_slots
                ],
            }

        return _StoredData(
            {
                "version": STORE_VERSION,
                RESTORE_KEY: {
                    PLAN_KEY: plan_payload,
                    HUMIDITY_DAILY_MAX_KEY: self._humidity_daily_max,
                    OBSERVATIONS_KEY: [
                        {
                            "day": ob.day.isoformat(),
                            "weekday": ob.weekday,
                            "category": ob.category,
                            "runtime_hours": ob.runtime_hours,
                            "valid": ob.valid,
                            "excluded_reason": ob.excluded_reason,
                            "coverage_ratio": ob.coverage_ratio,
                            "max_gap_minutes": ob.max_gap_minutes,
                            "active_segments": ob.active_segments,
                        }
                        for ob in self._observations
                    ],
                },
            }
        )

    async def _async_restore(self) -> None:
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        stored_samples = (
            await self._samples_store.async_load()
            if self._samples_store is not None
            else None
        )
        if isinstance(stored_samples, dict):
            self._set_power_samples(
                zip(stored_samples.get("t", []), stored_samples.get("v", [])), tz
            )

        restored = await self.async_get_last_extra_data()
        if restored is None:
            self._prune_history()
            return
        payload = restored.as_dict().get(RESTORE_KEY)
        if not isinstance(payload, dict):
            self._prune_history()
            return

        if stored_samples is None and payload.get(POWER_SAMPLES_KEY):
            self._set_power_samples(payload[POWER_SAMPLES_KEY], tz)
            self._schedule_samples_save()

        humidity = payload.get(HUMIDITY_DAILY_MAX_KEY)
        if isinstance(humidity, dict):
            self._humidity_daily_max = {
                str(day): float(value)
                for day, value in humidity.items()
                if isinstance(day, str)
            }

        restored_obs = []
        for row in payload.get(OBSERVATIONS_KEY, []):
            if not isinstance(row, dict):
                continue
            try:
                restored_obs.append(
                    DailyObservation(
                        day=date.fromisoformat(str(row["day"])),
                        weekday=int(row["weekday"]),
                        category=str(row["category"]),
                        runtime_hours=float(row["runtime_hours"]),
                        valid=bool(row["valid"]),
                        excluded_reason=(
                            str(row["excluded_reason"])
                            if row.get("excluded_reason") is not None
                            else None
                        ),
                        coverage_ratio=float(row["coverage_ratio"]),
                        max_gap_minutes=float(row["max_gap_minutes"]),
                        active_segments=int(row["active_segments"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._observations = restored_obs

        plan_data = payload.get(PLAN_KEY)
        if isinstance(plan_data, dict):
            try:
                selected_slots = []
                for slot in plan_data.get("selected_slots", []):
                    selected_slots.append(
                        {
                            "start": datetime.fromtimestamp(float(slot["start"]), tz),
                            "end": datetime.fromtimestamp(float(slot["end"]), tz),
                            "cost": float(slot["cost"]),
                            "duration_minutes": int(slot.get("duration_minutes", 15)),
                        }
                    )

                self._plan = SchedulePlan(
                    window_start=datetime.fromtimestamp(
                        float(plan_data["window_start"]), tz
                    ),
                    window_end=datetime.fromtimestamp(
                        float(plan_data["window_end"]), tz
                    ),
                    category=str(plan_data["category"]),
                    shower_detected_today=bool(plan_data["shower_detected_today"]),
                    required_runtime_hours=float(plan_data["required_runtime_hours"]),
                    required_slots=int(plan_data["required_slots"]),
                    selected_slots=tuple(
                        PlannedSlot(
                            start=slot["start"],
                            end=slot["end"],
                            cost=slot["cost"],
                            duration_minutes=slot["duration_minutes"],
                        )
                        for slot in selected_slots
                    ),
                    expected_cost=float(plan_data["expected_cost"]),
                    runtime_capped=bool(plan_data["runtime_capped"]),
                    complete=bool(plan_data["complete"]),
                    incomplete_reason=(
                        str(plan_data["incomplete_reason"])
                        if plan_data.get("incomplete_reason") is not None
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError, OSError):
                self._plan = None

        self._prune_history()

    @property
    def _is_config_complete(self) -> bool:
        return not self._missing_requirements()

    def _missing_requirements(self) -> list[str]:
        options = self._entry.options
        missing: list[str] = []

        power_entity = options.get(CONF_WATER_HEATER_POWER_ENTITY)
        humidity_entity = options.get(CONF_BATHROOM_HUMIDITY_ENTITY)
        power_w = options.get(CONF_WATER_HEATER_POWER_W)
        max_hours = options.get(CONF_WATER_HEATER_MAX_HOURS)

        if not power_entity:
            missing.append(CONF_WATER_HEATER_POWER_ENTITY)
        if not humidity_entity:
            missing.append(CONF_BATHROOM_HUMIDITY_ENTITY)
        if power_w in (None, ""):
            missing.append(CONF_WATER_HEATER_POWER_W)
        if max_hours in (None, ""):
            missing.append(CONF_WATER_HEATER_MAX_HOURS)

        if power_entity:
            state = self.hass.states.get(power_entity)
            if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                missing.append(f"{CONF_WATER_HEATER_POWER_ENTITY}_unavailable")
        if humidity_entity:
            state = self.hass.states.get(humidity_entity)
            if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                missing.append(f"{CONF_BATHROOM_HUMIDITY_ENTITY}_unavailable")

        return missing

    @callback
    def _handle_periodic_refresh(self, _now: datetime) -> None:
        self._capture_current_power_sample()
        self._capture_current_humidity_state()
        self._refresh_state_only()
        self._request_plan_if_needed()

    @callback
    def _handle_power_state_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        value = self._parse_float(new_state.state)
        if value is None:
            return
        self._add_power_sample(dt_util.now(), value)
        if self._transition_entity_id == self._entry.options.get(
            CONF_WATER_HEATER_POWER_ENTITY
        ):
            self._refresh_state_only()

    @callback
    def _handle_humidity_state_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        value = self._parse_float(new_state.state)
        if value is None:
            return
        day = dt_util.now().date().isoformat()
        self._humidity_daily_max[day] = max(
            self._humidity_daily_max.get(day, 0.0), value
        )
        self._prune_history()

    @callback
    def _handle_transition_state_change(self, _event: Event) -> None:
        self._refresh_state_only()

    @callback
    def _handle_slot_boundary(self, _moment: datetime) -> None:
        self._refresh_state_only()

    async def _handle_planning_time(self, _moment: datetime) -> None:
        await self._async_generate_plan()

    def _capture_current_power_sample(self) -> None:
        entity_id = self._entry.options.get(CONF_WATER_HEATER_POWER_ENTITY)
        if not entity_id:
            return
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        value = self._parse_float(state.state)
        if value is None:
            return
        self._add_power_sample(dt_util.now(), value)

    def _add_power_sample(self, ts: datetime, value: float) -> None:
        """Append a sample, keeping the deque sorted, and drop expired ones."""
        if self._power_samples and ts < self._power_samples[-1][0]:
            # The clock went backwards; keep the samples ordered by time.
            return
        self._power_samples.append((ts, value))
        self._prune_power_samples(ts)
        self._schedule_samples_save()

    def _prune_power_samples(self, now: datetime) -> None:
        cutoff = now - SAMPLE_RETENTION
        samples = self._power_samples
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def _set_power_samples(self, rows: Any, tz: Any) -> None:
        """Replace the samples with valid ``(timestamp, value)`` rows, sorted."""
        samples: list[tuple[datetime, float]] = []
        for row in rows:
            try:
                raw_ts, raw_value = row
                ts = datetime.fromtimestamp(float(raw_ts), tz)
                value = float(raw_value)
            except (TypeError, ValueError, OSError, OverflowError):
                continue
            if math.isfinite(value):
                samples.append((ts, value))
        samples.sort(key=lambda sample: sample[0])
        self._power_samples = deque(samples)

    def _schedule_samples_save(self) -> None:
        if self._samples_store is not None:
            self._samples_store.async_delay_save(
                self._samples_store_data, SAMPLES_SAVE_DELAY_S
            )

    def _capture_current_humidity_state(self) -> None:
        entity_id = self._entry.options.get(CONF_BATHROOM_HUMIDITY_ENTITY)
        if not entity_id:
            return
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        humidity = self._parse_float(state.state)
        if humidity is None:
            return
        day = dt_util.now().date().isoformat()
        self._humidity_daily_max[day] = max(
            self._humidity_daily_max.get(day, 0.0), humidity
        )

    def _prune_history(self) -> None:
        now = dt_util.now()
        self._prune_power_samples(now)

        day_cutoff = (now.date() - SAMPLE_RETENTION).isoformat()
        self._humidity_daily_max = {
            day: value
            for day, value in self._humidity_daily_max.items()
            if day >= day_cutoff
        }
        self._observations = [
            ob
            for ob in self._observations
            if ob.day >= now.date() - timedelta(days=ROLLING_WINDOW_DAYS)
        ]

    async def _async_generate_plan(self) -> None:
        if self._plan_lock.locked():
            return
        async with self._plan_lock:
            await self._async_generate_plan_locked()

    async def _async_generate_plan_locked(self) -> None:
        if not self._is_config_complete:
            self._plan = None
            self._refresh_state_only()
            return

        tz_name = self.hass.config.time_zone
        now = dt_util.now().astimezone(dt_util.get_time_zone(tz_name))
        self._capture_current_power_sample()
        self._capture_current_humidity_state()

        shower_days: set[date] = set()
        for day_text, humidity in self._humidity_daily_max.items():
            if humidity < SHOWER_HUMIDITY_PCT:
                continue
            try:
                shower_days.add(date.fromisoformat(day_text))
            except ValueError:
                continue

        thresholds, observations = await self.hass.async_add_executor_job(
            _learn_from_samples,
            list(self._power_samples),
            now.date(),
            tz_name,
            shower_days,
        )
        if self._removed:
            return
        self._thresholds = thresholds
        self._observations = observations

        shower_today = now.date() in shower_days
        category = "shower" if shower_today else "no_shower"

        estimate = estimate_runtime(
            observations,
            weekday=now.weekday(),
            category=category,
        )

        power_w = float(self._entry.options.get(CONF_WATER_HEATER_POWER_W, 0.0))
        max_hours = float(self._entry.options.get(CONF_WATER_HEATER_MAX_HOURS, 0.0))

        rates = list(self._price_sensor._rates)
        plan = build_schedule_plan(
            now_local=now,
            today_category=category,
            shower_detected_today=shower_today,
            required_runtime_hours=estimate.required_hours,
            max_hours=max_hours,
            rated_power_kw=power_w / 1000.0,
            raw_price_slots=rates,
            tz_name=self.hass.config.time_zone,
        )

        adequacy, adequacy_evidence = compute_max_hours_adequacy(
            observations, max_hours
        )

        self._plan = plan
        self._required_runtime_hours = estimate.required_hours
        self._required_slots = plan.required_slots
        self._runtime_source_level = estimate.source_level
        self._runtime_percentile = estimate.percentile_hours
        self._historical_samples = estimate.sample_count
        self._runtime_capped = plan.runtime_capped
        self._max_hours_adequacy = adequacy
        self._max_hours_evidence = adequacy_evidence
        self._plan_complete = plan.complete
        self._plan_incomplete_reason = plan.incomplete_reason
        self._last_calculation = now

        _LOGGER.debug(
            "Water-heater plan generated: category=%s required_hours=%.3f slots=%d "
            "selected=%d expected_cost=%.6f source=%s threshold=%s",
            category,
            estimate.required_hours,
            plan.required_slots,
            len(plan.selected_slots),
            plan.expected_cost,
            estimate.source_level,
            self._thresholds.confidence,
        )

        self._reschedule_slot_boundaries()
        self._refresh_state_only()

    def _reschedule_slot_boundaries(self) -> None:
        for unsubscribe in self._boundary_unsubs:
            unsubscribe()
        self._boundary_unsubs.clear()

        if self._plan is None:
            return

        now = dt_util.now()
        for slot in self._plan.selected_slots:
            if slot.start > now:
                self._boundary_unsubs.append(
                    async_track_point_in_time(
                        self.hass, self._handle_slot_boundary, slot.start
                    )
                )
            if slot.end > now:
                self._boundary_unsubs.append(
                    async_track_point_in_time(
                        self.hass, self._handle_slot_boundary, slot.end
                    )
                )

    def _recommended_state(self) -> str:
        now = dt_util.now().astimezone(
            dt_util.get_time_zone(self.hass.config.time_zone)
        )
        return plan_state_at(now, self._plan)

    def _refresh_state_only(self) -> None:
        self._state = (
            self._recommended_state() if self._is_config_complete else STATE_UNAVAILABLE
        )
        self.async_write_ha_state()
        for listener in tuple(self._update_listeners):
            listener()

    def async_add_update_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback that runs when the schedule diagnostics change."""
        self._update_listeners.append(listener)

        def _remove_listener() -> None:
            if listener in self._update_listeners:
                self._update_listeners.remove(listener)

        return _remove_listener

    @property
    def on_slots(self) -> list[dict[str, Any]]:
        """Return the planned ON slots."""
        if self._plan is None:
            return []
        return [
            {
                "start": slot.start.isoformat(),
                "end": slot.end.isoformat(),
                "cost": slot.cost,
                "duration": slot.duration_minutes,
            }
            for slot in self._plan.selected_slots
        ]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"on_slots": self.on_slots}

    @property
    def diagnostic_attributes(self) -> dict[str, Any]:
        options = self._entry.options
        missing_config = self._missing_requirements()

        now = dt_util.now().astimezone(
            dt_util.get_time_zone(self.hass.config.time_zone)
        )
        valid_count = sum(1 for ob in self._observations if ob.valid)
        excluded_count = len(self._observations) - valid_count
        threshold_confidence = self._thresholds.confidence

        active_entity = options.get(CONF_WATER_HEATER_ACTIVE_ENTITY)
        if active_entity and self.hass.states.get(active_entity) is not None:
            heater_state_source = "active_entity"
        elif options.get(CONF_WATER_HEATER_POWER_ENTITY):
            heater_state_source = "power_derived"
        else:
            heater_state_source = "unknown"

        return {
            "current_recommended_state": "on" if self._state == "on" else "off",
            "required_runtime_hours": self._required_runtime_hours,
            "required_slot_count": self._plan.required_slots if self._plan else 0,
            "selected_runtime_category": self._plan.category if self._plan else None,
            "shower_detected_today": (
                self._plan.shower_detected_today if self._plan else False
            ),
            "total_expected_schedule_cost": (
                self._plan.expected_cost if self._plan else 0.0
            ),
            "historical_sample_count": self._historical_samples,
            "runtime_percentile": self._runtime_percentile,
            "last_calculation_time": (
                self._last_calculation.isoformat() if self._last_calculation else None
            ),
            "runtime_capped": self._runtime_capped,
            "max_hours_adequacy": self._max_hours_adequacy,
            "plan_status": "complete" if self._plan_complete else "incomplete",
            "plan_incomplete_reason": self._plan_incomplete_reason,
            "valid_daily_count": valid_count,
            "excluded_daily_count": excluded_count,
            "learning_source_level": self._runtime_source_level,
            "heater_state_source": heater_state_source,
            "threshold_confidence": threshold_confidence,
            "threshold_active_heating_w": self._thresholds.active_heating_w,
            "threshold_non_heating_w": self._thresholds.non_heating_w,
            "threshold_sample_count": self._thresholds.sample_count,
            "max_gap_minutes_limit": 45,
            "min_daily_coverage_ratio": 0.9,
            "max_hours_adequacy_evidence": self._max_hours_evidence,
            "window_start": self._plan.window_start.isoformat() if self._plan else None,
            "window_end": self._plan.window_end.isoformat() if self._plan else None,
            "missing_config": missing_config,
            "plan_available": self._plan is not None,
            "updated_at": now.isoformat(),
        }

    @staticmethod
    def _parse_float(value: str) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None


class WaterHeaterDiagnosticsSensor(SensorEntity):
    """Expose water-heater planning and learning diagnostics."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        schedule_sensor: WaterHeaterScheduleSensor,
    ) -> None:
        self._schedule_sensor = schedule_sensor
        description = SensorEntityDescription(
            key="waterheaterdiagnostics",
            translation_key="waterheaterdiagnostics",
            icon="mdi:water-boiler-alert",
        )
        self.entity_description = description
        self.entity_id = PREFERRED_SENSOR_ENTITY_IDS[description.key]
        self._attr_suggested_object_id = description.key
        self._attr_unique_id = build_sensor_unique_id(entry, description.key)
        self._attr_device_info = device_info
        self._attr_exclude_from_recording = entry.options.get(
            CONF_EXCLUDE_FROM_RECORDING, True
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._schedule_sensor.async_add_update_listener(self._handle_update)
        )

    @property
    def native_value(self) -> str:
        """Return whether the current plan is complete."""
        return "complete" if self._schedule_sensor._plan_complete else "incomplete"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all water-heater diagnostics."""
        return self._schedule_sensor.diagnostic_attributes

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
