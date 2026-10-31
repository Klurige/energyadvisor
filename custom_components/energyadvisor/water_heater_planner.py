"""Water-heater planning primitives.

This module is intentionally Home-Assistant agnostic and deterministic.

Threshold detection strategy
----------------------------
We derive heating thresholds from the rolling 30-day power-value distribution
with a deterministic two-cluster split:

1. Build a fixed-width histogram (40 bins) from finite non-negative power
   samples.
2. Use Otsu's method to find the split that maximises between-class variance.
3. Compute weighted means for the low and high clusters.
4. Derive hysteresis thresholds from the class separation:
   - non_heating_threshold = low_mean + 0.35 * separation
   - active_heating_threshold = low_mean + 0.75 * separation

Confidence is "insufficient" unless all checks pass:
- minimum sample count,
- both clusters non-empty and sufficiently large,
- sufficient power separation.

Assumptions/limits
------------------
- The heater draws distinctly higher power while actively heating.
- If loads overlap heavily with heater power or the heater has highly variable
  modulation near standby levels, clusters can merge and confidence drops.
- We never use arbitrary fallback thresholds; low confidence disables learning.

Observation quality rules
-------------------------
- Maximum allowed sample gap: 45 minutes.
- Minimum daily coverage: 90%.
Coverage treats each gap as "known" for at most 45 minutes; extra time counts
as missing. Days below thresholds are excluded from learning.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import math
from typing import Literal

from homeassistant.util import dt as dt_util

DEFAULT_RUNTIME_HOURS = 4.0
PLANNING_HOUR = 20
SLOT_MINUTES = 15
SLOT_HOURS = SLOT_MINUTES / 60.0
ROLLING_WINDOW_DAYS = 30
MIN_LEARNED_SAMPLES = 10
FULL_LEARNED_SAMPLES = 20
PERCENTILE = 95
MAX_GAP = timedelta(minutes=45)
MIN_COVERAGE_RATIO = 0.90
THRESHOLD_MIN_SAMPLES = 120
THRESHOLD_MIN_CLUSTER_SHARE = 0.05
THRESHOLD_MIN_SEPARATION_W = 250.0
THRESHOLD_HISTOGRAM_BINS = 40

Category = Literal["no_shower", "shower"]
SourceLevel = Literal[
    "same_weekday_same_category",
    "same_weekday_any_category",
    "other_weekday_same_category",
    "all_observations",
    "default",
]


@dataclass(frozen=True)
class ThresholdResult:
    """Derived thresholds and confidence diagnostics."""

    available: bool
    active_heating_w: float | None
    non_heating_w: float | None
    confidence: Literal[
        "ready",
        "insufficient_samples",
        "insufficient_cluster_balance",
        "insufficient_separation",
    ]
    sample_count: int
    low_mean_w: float | None = None
    high_mean_w: float | None = None


@dataclass(frozen=True)
class DailyObservation:
    """One local-calendar-day learning candidate."""

    day: date
    weekday: int
    category: Category
    runtime_hours: float
    valid: bool
    excluded_reason: str | None
    coverage_ratio: float
    max_gap_minutes: float
    active_segments: int


@dataclass(frozen=True)
class RuntimeEstimate:
    """Estimated runtime and diagnostics."""

    required_hours: float
    percentile_hours: float | None
    sample_count: int
    source_level: SourceLevel


@dataclass(frozen=True)
class PlannedSlot:
    """One ON slot in the output schedule."""

    start: datetime
    end: datetime
    cost: float
    duration_minutes: int = SLOT_MINUTES


@dataclass(frozen=True)
class SchedulePlan:
    """Complete fixed plan for one 20:00->20:00 window."""

    window_start: datetime
    window_end: datetime
    category: Category
    shower_detected_today: bool
    required_runtime_hours: float
    required_slots: int
    selected_slots: tuple[PlannedSlot, ...]
    expected_cost: float
    runtime_capped: bool
    complete: bool
    incomplete_reason: str | None


def _finite_non_negative(value: float | int | None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def nearest_rank_percentile(values: list[float], percentile: int) -> float | None:
    """Return nearest-rank percentile value."""
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil((percentile / 100.0) * len(ordered))
    rank = min(max(rank, 1), len(ordered))
    return ordered[rank - 1]


def derive_thresholds(samples_w: list[float]) -> ThresholdResult:
    """Derive active/non-heating thresholds from power distribution."""
    values = [
        v
        for v in (_finite_non_negative(sample) for sample in samples_w)
        if v is not None
    ]
    sample_count = len(values)
    if sample_count < THRESHOLD_MIN_SAMPLES:
        return ThresholdResult(False, None, None, "insufficient_samples", sample_count)

    min_v = min(values)
    max_v = max(values)
    if max_v <= min_v:
        return ThresholdResult(
            False,
            None,
            None,
            "insufficient_separation",
            sample_count,
            low_mean_w=min_v,
            high_mean_w=max_v,
        )

    width = (max_v - min_v) / THRESHOLD_HISTOGRAM_BINS
    if width <= 0:
        return ThresholdResult(
            False, None, None, "insufficient_separation", sample_count
        )

    bins = [0] * THRESHOLD_HISTOGRAM_BINS
    for value in values:
        index = int((value - min_v) / width)
        if index >= THRESHOLD_HISTOGRAM_BINS:
            index = THRESHOLD_HISTOGRAM_BINS - 1
        bins[index] += 1

    centers = [min_v + (i + 0.5) * width for i in range(THRESHOLD_HISTOGRAM_BINS)]
    total = float(sample_count)
    sum_total = sum(count * center for count, center in zip(bins, centers))

    sum_b = 0.0
    w_b = 0.0
    best_index = -1
    best_var = -1.0
    for index, (count, center) in enumerate(zip(bins, centers)):
        w_b += count
        if w_b <= 0:
            continue
        w_f = total - w_b
        if w_f <= 0:
            break
        sum_b += count * center
        mean_b = sum_b / w_b
        mean_f = (sum_total - sum_b) / w_f
        between = w_b * w_f * (mean_b - mean_f) ** 2
        if between > best_var:
            best_var = between
            best_index = index

    if best_index < 0:
        return ThresholdResult(
            False, None, None, "insufficient_cluster_balance", sample_count
        )

    low_count = sum(bins[: best_index + 1])
    high_count = sample_count - low_count
    if low_count <= 0 or high_count <= 0:
        return ThresholdResult(
            False, None, None, "insufficient_cluster_balance", sample_count
        )

    if (
        low_count / sample_count < THRESHOLD_MIN_CLUSTER_SHARE
        or high_count / sample_count < THRESHOLD_MIN_CLUSTER_SHARE
    ):
        return ThresholdResult(
            False, None, None, "insufficient_cluster_balance", sample_count
        )

    low_mean = (
        sum(
            count * center
            for count, center in zip(bins[: best_index + 1], centers[: best_index + 1])
        )
        / low_count
    )
    high_mean = (
        sum(
            count * center
            for count, center in zip(bins[best_index + 1 :], centers[best_index + 1 :])
        )
        / high_count
    )

    separation = high_mean - low_mean
    if separation < THRESHOLD_MIN_SEPARATION_W:
        return ThresholdResult(
            False,
            None,
            None,
            "insufficient_separation",
            sample_count,
            low_mean_w=low_mean,
            high_mean_w=high_mean,
        )

    non_heating = low_mean + 0.35 * separation
    active_heating = low_mean + 0.75 * separation

    if active_heating <= non_heating:
        return ThresholdResult(
            False,
            None,
            None,
            "insufficient_separation",
            sample_count,
            low_mean_w=low_mean,
            high_mean_w=high_mean,
        )

    return ThresholdResult(
        True,
        active_heating_w=active_heating,
        non_heating_w=non_heating,
        confidence="ready",
        sample_count=sample_count,
        low_mean_w=low_mean,
        high_mean_w=high_mean,
    )


def build_daily_observation(
    *,
    day: date,
    tz_name: str,
    power_samples: list[tuple[datetime, float]],
    shower_detected: bool,
    thresholds: ThresholdResult,
) -> DailyObservation:
    """Build one day observation from timestamped power samples."""
    day_start_local = datetime.combine(day, time.min, dt_util.get_time_zone(tz_name))
    day_end_local = day_start_local + timedelta(days=1)

    samples = sorted(
        (
            (ts.astimezone(day_start_local.tzinfo), value)
            for ts, value in power_samples
            if day_start_local - MAX_GAP
            <= ts.astimezone(day_start_local.tzinfo)
            <= day_end_local + MAX_GAP
        ),
        key=lambda item: item[0],
    )

    category: Category = "shower" if shower_detected else "no_shower"
    if not thresholds.available:
        return DailyObservation(
            day=day,
            weekday=day.weekday(),
            category=category,
            runtime_hours=0.0,
            valid=False,
            excluded_reason=f"threshold_{thresholds.confidence}",
            coverage_ratio=0.0,
            max_gap_minutes=24 * 60,
            active_segments=0,
        )

    # Find latest sample before day start as initial value.
    initial_value: float | None = None
    for ts, value in samples:
        if ts <= day_start_local:
            initial_value = value
        else:
            break

    timeline: list[tuple[datetime, float]] = []
    if initial_value is not None:
        timeline.append((day_start_local, initial_value))

    for ts, value in samples:
        if day_start_local <= ts <= day_end_local:
            timeline.append((ts, value))

    if not timeline:
        return DailyObservation(
            day=day,
            weekday=day.weekday(),
            category=category,
            runtime_hours=0.0,
            valid=False,
            excluded_reason="missing_power",
            coverage_ratio=0.0,
            max_gap_minutes=24 * 60,
            active_segments=0,
        )

    if timeline[-1][0] < day_end_local:
        timeline.append((day_end_local, timeline[-1][1]))

    max_gap_seconds = 0.0
    missing_seconds = 0.0
    runtime_seconds = 0.0
    active_segments = 0
    active = False

    # Unknown period at the start of day when no sample exists before day start.
    if timeline[0][0] > day_start_local:
        start_gap = (timeline[0][0] - day_start_local).total_seconds()
        max_gap_seconds = max(max_gap_seconds, start_gap)
        missing_seconds += start_gap

    for (start, value), (end, _next_value) in zip(timeline, timeline[1:]):
        if end <= start:
            continue
        gap = (end - start).total_seconds()
        # State-change streams can legitimately have long unchanged periods.
        # Treat them as covered once a value is known.
        max_gap_seconds = max(max_gap_seconds, min(gap, MAX_GAP.total_seconds()))

        if not active and value >= (thresholds.active_heating_w or math.inf):
            active = True
            active_segments += 1
        elif active and value <= (thresholds.non_heating_w or -math.inf):
            active = False

        if active:
            runtime_seconds += gap

    day_seconds = (day_end_local - day_start_local).total_seconds()
    coverage_ratio = max(0.0, min(1.0, (day_seconds - missing_seconds) / day_seconds))
    max_gap_minutes = max_gap_seconds / 60.0

    if coverage_ratio < MIN_COVERAGE_RATIO:
        return DailyObservation(
            day=day,
            weekday=day.weekday(),
            category=category,
            runtime_hours=runtime_seconds / 3600.0,
            valid=False,
            excluded_reason="insufficient_coverage",
            coverage_ratio=coverage_ratio,
            max_gap_minutes=max_gap_minutes,
            active_segments=active_segments,
        )

    if max_gap_seconds > MAX_GAP.total_seconds():
        return DailyObservation(
            day=day,
            weekday=day.weekday(),
            category=category,
            runtime_hours=runtime_seconds / 3600.0,
            valid=False,
            excluded_reason="excessive_gap",
            coverage_ratio=coverage_ratio,
            max_gap_minutes=max_gap_minutes,
            active_segments=active_segments,
        )

    # Conservative low-demand safeguard: tiny runtime with no clear cycles.
    runtime_hours = runtime_seconds / 3600.0
    if runtime_hours < 0.5 and active_segments <= 1:
        return DailyObservation(
            day=day,
            weekday=day.weekday(),
            category=category,
            runtime_hours=runtime_hours,
            valid=False,
            excluded_reason="low_confidence_low_demand",
            coverage_ratio=coverage_ratio,
            max_gap_minutes=max_gap_minutes,
            active_segments=active_segments,
        )

    return DailyObservation(
        day=day,
        weekday=day.weekday(),
        category=category,
        runtime_hours=runtime_hours,
        valid=True,
        excluded_reason=None,
        coverage_ratio=coverage_ratio,
        max_gap_minutes=max_gap_minutes,
        active_segments=active_segments,
    )


def _sample_time(sample: tuple[datetime, float]) -> datetime:
    return sample[0]


def build_daily_observations(
    *,
    today: date,
    tz_name: str,
    power_samples: Sequence[tuple[datetime, float]],
    shower_days: Collection[date],
    thresholds: ThresholdResult,
    days: int = ROLLING_WINDOW_DAYS,
) -> list[DailyObservation]:
    """Build one observation for each of the ``days`` days before ``today``.

    ``power_samples`` must be sorted by time. Each day only looks at the
    samples within ``MAX_GAP`` of it, so the total work grows linearly with
    the number of samples instead of with samples times days.
    """
    tz = dt_util.get_time_zone(tz_name)
    observations: list[DailyObservation] = []
    for offset in range(days, 0, -1):
        day = today - timedelta(days=offset)
        day_start = datetime.combine(day, time.min, tz)
        day_end = day_start + timedelta(days=1)
        first = bisect_left(power_samples, day_start - MAX_GAP, key=_sample_time)
        last = bisect_right(power_samples, day_end + MAX_GAP, key=_sample_time)
        observations.append(
            build_daily_observation(
                day=day,
                tz_name=tz_name,
                power_samples=list(power_samples[first:last]),
                shower_detected=day in shower_days,
                thresholds=thresholds,
            )
        )
    return observations


def _select_learning_set(
    observations: list[DailyObservation],
    weekday: int,
    category: Category,
) -> tuple[list[DailyObservation], SourceLevel]:
    valid = [ob for ob in observations if ob.valid]
    levels: list[tuple[SourceLevel, list[DailyObservation]]] = [
        (
            "same_weekday_same_category",
            [ob for ob in valid if ob.weekday == weekday and ob.category == category],
        ),
        (
            "same_weekday_any_category",
            [ob for ob in valid if ob.weekday == weekday],
        ),
        (
            "other_weekday_same_category",
            [ob for ob in valid if ob.weekday != weekday and ob.category == category],
        ),
        ("all_observations", valid),
    ]

    for level, candidates in levels:
        if len(candidates) >= MIN_LEARNED_SAMPLES:
            return candidates, level

    return [], "default"


def estimate_runtime(
    observations: list[DailyObservation],
    *,
    weekday: int,
    category: Category,
) -> RuntimeEstimate:
    """Estimate runtime from valid observations with fallback hierarchy."""
    candidates, source_level = _select_learning_set(observations, weekday, category)
    if not candidates:
        return RuntimeEstimate(
            required_hours=DEFAULT_RUNTIME_HOURS,
            percentile_hours=None,
            sample_count=0,
            source_level="default",
        )

    runtimes = [
        ob.runtime_hours for ob in candidates if math.isfinite(ob.runtime_hours)
    ]
    if not runtimes:
        return RuntimeEstimate(
            required_hours=DEFAULT_RUNTIME_HOURS,
            percentile_hours=None,
            sample_count=len(candidates),
            source_level=source_level,
        )

    percentile_value = nearest_rank_percentile(runtimes, PERCENTILE)
    learned = percentile_value if percentile_value is not None else max(runtimes)
    sample_count = len(runtimes)

    if sample_count < MIN_LEARNED_SAMPLES:
        runtime = DEFAULT_RUNTIME_HOURS
    elif sample_count < FULL_LEARNED_SAMPLES:
        weight = (sample_count - MIN_LEARNED_SAMPLES) / (
            FULL_LEARNED_SAMPLES - MIN_LEARNED_SAMPLES
        )
        runtime = (DEFAULT_RUNTIME_HOURS * (1.0 - weight)) + (learned * weight)
    else:
        runtime = learned

    return RuntimeEstimate(
        required_hours=runtime,
        percentile_hours=learned,
        sample_count=sample_count,
        source_level=source_level,
    )


def compute_max_hours_adequacy(
    observations: list[DailyObservation], max_hours: float
) -> tuple[Literal["sufficient", "insufficient", "unknown"], dict[str, int]]:
    """Assess max-hours adequacy from repeated demand evidence."""
    valid = [ob for ob in observations if ob.valid]
    if len(valid) < 10:
        return "unknown", {"valid_days": len(valid), "exceed_days": 0}

    exceed_days = sum(1 for ob in valid if ob.runtime_hours > max_hours + 0.01)
    if exceed_days >= 3:
        return "insufficient", {"valid_days": len(valid), "exceed_days": exceed_days}
    if exceed_days == 0:
        return "sufficient", {"valid_days": len(valid), "exceed_days": exceed_days}
    return "unknown", {"valid_days": len(valid), "exceed_days": exceed_days}


def _parse_slot_datetime(value: object, tz_name: str) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = dt_util.parse_datetime(value)
        if dt is None:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_util.get_time_zone(tz_name))
    return dt.astimezone(dt_util.get_time_zone(tz_name))


def _extract_price_slot(
    slot: dict,
    *,
    tz_name: str,
    window_start: datetime,
    window_end: datetime,
) -> PlannedSlot | None:
    start = _parse_slot_datetime(slot.get("start") or slot.get("from"), tz_name)
    if start is None:
        return None

    end = _parse_slot_datetime(slot.get("end") or slot.get("to"), tz_name)
    if end is None:
        end = start + timedelta(minutes=SLOT_MINUTES)

    if end - start != timedelta(minutes=SLOT_MINUTES):
        return None

    cost = _finite_non_negative(slot.get("cost"))
    if cost is None:
        return None

    if start < window_start or end > window_end:
        return None

    return PlannedSlot(start=start, end=end, cost=cost)


def build_schedule_plan(
    *,
    now_local: datetime,
    today_category: Category,
    shower_detected_today: bool,
    required_runtime_hours: float,
    max_hours: float,
    rated_power_kw: float,
    raw_price_slots: list[dict],
    tz_name: str,
) -> SchedulePlan:
    """Create the fixed cheapest ON-slot plan for the current planning window.

    Slots that have already ended at ``now_local`` are never selected, so a
    plan made in the middle of a window (for example after a restart) only
    uses the time that is left.
    """
    today_2000 = datetime.combine(
        now_local.date(), time(PLANNING_HOUR, 0), tzinfo=now_local.tzinfo
    )
    if now_local < today_2000:
        window_start = today_2000 - timedelta(days=1)
    else:
        window_start = today_2000
    window_end = window_start + timedelta(days=1)

    required_hours = max(0.0, required_runtime_hours)
    required_slots = math.ceil(required_hours / SLOT_HOURS)
    max_slots = max(0, int(math.floor(max_hours / SLOT_HOURS + 1e-9)))

    runtime_capped = required_slots > max_slots
    target_slots = min(required_slots, max_slots)

    eligible: list[PlannedSlot] = []
    for slot in raw_price_slots:
        extracted = _extract_price_slot(
            slot,
            tz_name=tz_name,
            window_start=window_start,
            window_end=window_end,
        )
        if extracted is not None and extracted.end > now_local:
            eligible.append(extracted)

    eligible.sort(key=lambda slot: (slot.cost, slot.start))
    selected = tuple(sorted(eligible[:target_slots], key=lambda slot: slot.start))

    incomplete_reason: str | None = None
    complete = True
    if runtime_capped:
        complete = False
        incomplete_reason = "runtime_capped"
    if len(eligible) < target_slots:
        complete = False
        incomplete_reason = "insufficient_slots"

    expected_cost = sum(slot.cost * rated_power_kw * SLOT_HOURS for slot in selected)

    return SchedulePlan(
        window_start=window_start,
        window_end=window_end,
        category=today_category,
        shower_detected_today=shower_detected_today,
        required_runtime_hours=required_hours,
        required_slots=required_slots,
        selected_slots=selected,
        expected_cost=expected_cost,
        runtime_capped=runtime_capped,
        complete=complete,
        incomplete_reason=incomplete_reason,
    )


def plan_state_at(now_local: datetime, plan: SchedulePlan | None) -> str:
    """Return recommended state for current time from fixed ON slots."""
    if plan is None:
        return "off"
    for slot in plan.selected_slots:
        if slot.start <= now_local < slot.end:
            return "on"
    return "off"
