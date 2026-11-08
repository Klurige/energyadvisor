"""Heating demand model for a heat pump with direct-electric backup.

The model is a change-point degree-hour regression (energy signature):

    P = d + (a + b * wind) * max(0, T_base - T_eff) + c * max(0, T_backup - T_eff)

``T_eff`` is the outdoor temperature smoothed with an exponential moving
average, which stands in for the thermal inertia of the building. The wind
term scales with the temperature difference because infiltration losses grow
with both. The second hinge at ``T_backup`` lets the curve steepen on the
coldest days when the direct-electric backup takes over. The result is then
scaled by a learned hour-of-day profile (workday or weekend) that captures
night setback and similar schedules.

``T_base``, ``T_backup`` and the time constant are found by grid search; the
coefficients by weighted least squares with all coefficients kept
non-negative. The module has no Home Assistant dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
import math
from typing import Any

TAU_GRID_HOURS: tuple[float, ...] = (0.0, 3.0, 6.0, 12.0, 24.0, 48.0)
BALANCE_GRID_C: tuple[float, ...] = tuple(float(t) for t in range(8, 21, 2))
BACKUP_GRID_C: tuple[float, ...] = tuple(float(t) for t in range(-24, 3, 3))
BALANCE_REFINE_OFFSETS_C: tuple[float, ...] = (-1.5, -1.0, -0.5, 0.5, 1.0, 1.5)
MIN_BACKUP_GAP_C = 4.0
RECENCY_HALF_LIFE_DAYS = 120.0
MIN_TRAINING_HOURS = 7 * 24
MIN_HEATING_HOURS = 48
HEATING_ACTIVE_KW = 0.05
WIND_MIN_COVERAGE = 0.25
SMOOTHING_MAX_GAP_HOURS = 6
PROFILE_PRIOR_HOURS = 24.0
PROFILE_MIN = 0.5
PROFILE_MAX = 2.0
FALLBACK_BALANCE_C = 15.0
FALLBACK_DESIGN_C = -15.0
FALLBACK_TIME_CONSTANT_H = 12.0
DAY_TYPES: tuple[str, ...] = ("workday", "weekend")

_FEATURE_COUNT = 4  # standby, degree-hours, wind x degree-hours, backup


def day_type_for(weekday: int) -> str:
    """Map a Python weekday (Monday=0) to a profile day type."""
    return "weekend" if weekday >= 5 else "workday"


def smooth_temperatures(
    temps_c: list[float | None],
    time_constant_h: float,
    max_gap_hours: int = SMOOTHING_MAX_GAP_HOURS,
) -> list[float | None]:
    """Return the exponentially smoothed temperature for a contiguous hourly series.

    Short gaps carry the smoothed value forward; after a longer gap the
    average restarts from the next observed value.
    """
    alpha = 1.0 if time_constant_h <= 0.0 else 1.0 - math.exp(-1.0 / time_constant_h)
    smoothed: list[float | None] = []
    state: float | None = None
    gap = 0
    for temp in temps_c:
        if temp is None:
            gap += 1
            if gap > max_gap_hours:
                state = None
            smoothed.append(state)
            continue
        gap = 0
        state = temp if state is None else state + alpha * (temp - state)
        smoothed.append(state)
    return smoothed


@dataclass(slots=True)
class HeatingModel:
    """A fitted (or fallback) heating model."""

    balance_temp_c: float
    backup_temp_c: float | None
    time_constant_h: float
    base_kw: float = 0.0
    slope_kw_per_k: float = 0.0
    wind_kw_per_k_ms: float = 0.0
    backup_kw_per_k: float = 0.0
    mean_wind_ms: float = 0.0
    uses_wind: bool = False
    fitted: bool = False
    training_hours: int = 0
    mae_kw: float | None = None
    r2: float | None = None
    profile: dict[str, list[float]] = field(default_factory=dict)

    def base_kw_at(self, temp_eff_c: float, wind_ms: float | None) -> float:
        """Return the modelled power before the hour-of-day profile."""
        degree = max(0.0, self.balance_temp_c - temp_eff_c)
        wind = self.mean_wind_ms if wind_ms is None else max(0.0, wind_ms)
        power = self.base_kw + self.slope_kw_per_k * degree
        if self.uses_wind:
            power += self.wind_kw_per_k_ms * wind * degree
        if self.backup_temp_c is not None:
            power += self.backup_kw_per_k * max(0.0, self.backup_temp_c - temp_eff_c)
        return max(0.0, power)

    def profile_factor(self, local_hour: int, day_type: str) -> float:
        """Return the learned hour-of-day multiplier."""
        factors = self.profile.get(day_type)
        if not factors or not 0 <= local_hour < len(factors):
            return 1.0
        return factors[local_hour]

    def predict_kw(
        self,
        temp_eff_c: float,
        wind_ms: float | None,
        local_hour: int,
        day_type: str,
    ) -> float:
        """Return the forecast heating power in kW for one hour."""
        return self.base_kw_at(temp_eff_c, wind_ms) * self.profile_factor(
            local_hour, day_type
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize the model for storage and diagnostics."""
        return {
            "balance_temp_c": self.balance_temp_c,
            "backup_temp_c": self.backup_temp_c,
            "time_constant_h": self.time_constant_h,
            "base_kw": self.base_kw,
            "slope_kw_per_k": self.slope_kw_per_k,
            "wind_kw_per_k_ms": self.wind_kw_per_k_ms,
            "backup_kw_per_k": self.backup_kw_per_k,
            "mean_wind_ms": self.mean_wind_ms,
            "uses_wind": self.uses_wind,
            "fitted": self.fitted,
            "training_hours": self.training_hours,
            "mae_kw": self.mae_kw,
            "r2": self.r2,
            "profile": {key: list(values) for key, values in self.profile.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HeatingModel | None:
        """Restore a model serialized with :meth:`as_dict`."""
        try:
            backup = data.get("backup_temp_c")
            profile_data = data.get("profile") or {}
            profile = {
                str(key): [float(v) for v in values]
                for key, values in profile_data.items()
                if isinstance(values, list) and len(values) == 24
            }
            return cls(
                balance_temp_c=float(data["balance_temp_c"]),
                backup_temp_c=None if backup is None else float(backup),
                time_constant_h=float(data["time_constant_h"]),
                base_kw=float(data.get("base_kw", 0.0)),
                slope_kw_per_k=float(data.get("slope_kw_per_k", 0.0)),
                wind_kw_per_k_ms=float(data.get("wind_kw_per_k_ms", 0.0)),
                backup_kw_per_k=float(data.get("backup_kw_per_k", 0.0)),
                mean_wind_ms=float(data.get("mean_wind_ms", 0.0)),
                uses_wind=bool(data.get("uses_wind", False)),
                fitted=bool(data.get("fitted", False)),
                training_hours=int(data.get("training_hours", 0)),
                mae_kw=_optional_float(data.get("mae_kw")),
                r2=_optional_float(data.get("r2")),
                profile=profile,
            )
        except (KeyError, TypeError, ValueError):
            return None


def _optional_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def fallback_model(rated_power_kw: float | None) -> HeatingModel:
    """Return a cold-start model derived from the rated heating power.

    The rated power is assumed to be drawn at the design temperature and the
    demand falls linearly to zero at the balance temperature.
    """
    slope = 0.0
    if rated_power_kw is not None and rated_power_kw > 0.0:
        slope = rated_power_kw / (FALLBACK_BALANCE_C - FALLBACK_DESIGN_C)
    return HeatingModel(
        balance_temp_c=FALLBACK_BALANCE_C,
        backup_temp_c=None,
        time_constant_h=FALLBACK_TIME_CONSTANT_H,
        slope_kw_per_k=slope,
        fitted=False,
    )


@dataclass(slots=True)
class _TrainingRow:
    temps_eff: tuple[float, ...]  # one per tau in TAU_GRID_HOURS
    wind_ms: float
    heating_kw: float
    weight: float
    local_hour: int
    day_type: str


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Solve a small linear system with partial pivoting."""
    size = len(rhs)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    scale = max((abs(aug[i][i]) for i in range(size)), default=0.0)
    tolerance = max(1e-12, scale * 1e-10)
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) <= tolerance:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for row in range(col + 1, size):
            factor = aug[row][col] / aug[col][col]
            if factor:
                for k in range(col, size + 1):
                    aug[row][k] -= factor * aug[col][k]
    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        acc = aug[row][size] - sum(
            aug[row][k] * solution[k] for k in range(row + 1, size)
        )
        solution[row] = acc / aug[row][row]
    return solution


def _nonnegative_least_squares(
    xtx: list[list[float]],
    xty: list[float],
    yty: float,
    allowed: tuple[int, ...],
) -> tuple[list[float], float]:
    """Return the best non-negative coefficients and their weighted SSE.

    With at most four features every subset can be tried; the best subset
    whose unconstrained solution is non-negative is the constrained optimum.
    """
    best_coefficients = [0.0] * _FEATURE_COUNT
    best_sse = yty
    for size in range(1, len(allowed) + 1):
        for subset in combinations(allowed, size):
            sub_matrix = [[xtx[i][j] for j in subset] for i in subset]
            sub_rhs = [xty[i] for i in subset]
            solution = _solve(sub_matrix, sub_rhs)
            if solution is None or any(value < 0.0 for value in solution):
                continue
            # SSE = y'Wy - 2 beta'X'Wy + beta'X'WX beta = y'Wy - beta'X'Wy at the optimum.
            sse = yty - sum(beta * sub_rhs[k] for k, beta in enumerate(solution))
            if sse < best_sse - 1e-12:
                best_sse = sse
                best_coefficients = [0.0] * _FEATURE_COUNT
                for k, index in enumerate(subset):
                    best_coefficients[index] = solution[k]
    return best_coefficients, best_sse


def _fit_candidate(
    rows: list[_TrainingRow],
    tau_index: int,
    balance_c: float,
    backup_c: float | None,
    use_wind: bool,
) -> tuple[list[float], float]:
    """Fit the coefficients for one (tau, balance, backup) combination."""
    xtx = [[0.0] * _FEATURE_COUNT for _ in range(_FEATURE_COUNT)]
    xty = [0.0] * _FEATURE_COUNT
    yty = 0.0
    for row in rows:
        temp = row.temps_eff[tau_index]
        degree = balance_c - temp
        if degree < 0.0:
            degree = 0.0
        backup = 0.0
        if backup_c is not None and temp < backup_c:
            backup = backup_c - temp
        features = (1.0, degree, degree * row.wind_ms, backup)
        weight = row.weight
        y = row.heating_kw
        for i in range(_FEATURE_COUNT):
            weighted = weight * features[i]
            if weighted == 0.0:
                continue
            xty[i] += weighted * y
            row_i = xtx[i]
            for j in range(i, _FEATURE_COUNT):
                row_i[j] += weighted * features[j]
        yty += weight * y * y
    for i in range(_FEATURE_COUNT):
        for j in range(i):
            xtx[i][j] = xtx[j][i]
    allowed = [0, 1]
    if use_wind:
        allowed.append(2)
    if backup_c is not None:
        allowed.append(3)
    return _nonnegative_least_squares(xtx, xty, yty, tuple(allowed))


def fit_heating_model(
    temps_c: list[float | None],
    winds_ms: list[float | None],
    heating_kw: list[float | None],
    local_hours: list[int],
    day_types: list[str],
    ages_days: list[float],
) -> HeatingModel | None:
    """Fit the heating model on a contiguous hourly series.

    All lists are aligned on the same hourly timeline (oldest first). Returns
    ``None`` when there is too little heating history to fit a model.
    """
    count = len(temps_c)
    if not (
        count
        == len(winds_ms)
        == len(heating_kw)
        == len(local_hours)
        == len(day_types)
        == len(ages_days)
    ):
        raise ValueError("Training series must have equal lengths")

    smoothed_by_tau = [smooth_temperatures(temps_c, tau) for tau in TAU_GRID_HOURS]

    wind_values = [
        (wind, ages_days[i])
        for i, wind in enumerate(winds_ms)
        if wind is not None and math.isfinite(wind) and heating_kw[i] is not None
    ]
    observed_hours = sum(1 for value in heating_kw if value is not None)
    use_wind = observed_hours > 0 and (
        len(wind_values) / observed_hours >= WIND_MIN_COVERAGE
    )
    mean_wind = 0.0
    if wind_values:
        weights = [0.5 ** (age / RECENCY_HALF_LIFE_DAYS) for _wind, age in wind_values]
        total = sum(weights)
        if total > 0.0:
            mean_wind = (
                sum(max(0.0, wind) * w for (wind, _), w in zip(wind_values, weights))
                / total
            )

    rows: list[_TrainingRow] = []
    for i in range(count):
        heating = heating_kw[i]
        if heating is None or not math.isfinite(heating) or heating < 0.0:
            continue
        temps_eff = tuple(series[i] for series in smoothed_by_tau)
        if any(temp is None for temp in temps_eff):
            continue
        wind = winds_ms[i]
        rows.append(
            _TrainingRow(
                temps_eff=temps_eff,  # type: ignore[arg-type]
                wind_ms=(
                    max(0.0, wind)
                    if wind is not None and math.isfinite(wind)
                    else mean_wind
                ),
                heating_kw=heating,
                weight=0.5 ** (max(0.0, ages_days[i]) / RECENCY_HALF_LIFE_DAYS),
                local_hour=local_hours[i],
                day_type=day_types[i],
            )
        )

    heating_hours = sum(1 for row in rows if row.heating_kw > HEATING_ACTIVE_KW)
    if len(rows) < MIN_TRAINING_HOURS or heating_hours < MIN_HEATING_HOURS:
        return None

    def sse_for(
        tau_index: int, balance_c: float, backup_c: float | None
    ) -> tuple[float, list[float]]:
        coefficients, sse = _fit_candidate(
            rows, tau_index, balance_c, backup_c, use_wind
        )
        return sse, coefficients

    # Stage 1: time constant and balance temperature without the backup hinge.
    best: tuple[float, int, float, float | None, list[float]] | None = None
    for tau_index in range(len(TAU_GRID_HOURS)):
        for balance_c in BALANCE_GRID_C:
            sse, coefficients = sse_for(tau_index, balance_c, None)
            if best is None or sse < best[0]:
                best = (sse, tau_index, balance_c, None, coefficients)
    assert best is not None
    _sse, tau_index, balance_c, _backup, _coefficients = best

    # Stage 2: the backup hinge for the chosen curve.
    for backup_c in BACKUP_GRID_C:
        if backup_c > balance_c - MIN_BACKUP_GAP_C:
            continue
        sse, coefficients = sse_for(tau_index, balance_c, backup_c)
        if sse < best[0]:
            best = (sse, tau_index, balance_c, backup_c, coefficients)

    # Stage 3: refine the balance temperature with the chosen hinge.
    backup_c = best[3]
    for offset in BALANCE_REFINE_OFFSETS_C:
        candidate = balance_c + offset
        if backup_c is not None and backup_c > candidate - MIN_BACKUP_GAP_C:
            continue
        sse, coefficients = sse_for(tau_index, candidate, backup_c)
        if sse < best[0]:
            best = (sse, tau_index, candidate, backup_c, coefficients)

    _sse, tau_index, balance_c, backup_c, coefficients = best
    if backup_c is not None and coefficients[3] <= 0.0:
        backup_c = None
    model = HeatingModel(
        balance_temp_c=balance_c,
        backup_temp_c=backup_c,
        time_constant_h=TAU_GRID_HOURS[tau_index],
        base_kw=coefficients[0],
        slope_kw_per_k=coefficients[1],
        wind_kw_per_k_ms=coefficients[2],
        backup_kw_per_k=coefficients[3] if backup_c is not None else 0.0,
        mean_wind_ms=mean_wind,
        uses_wind=use_wind and coefficients[2] > 0.0,
        fitted=True,
        training_hours=len(rows),
    )

    model.profile = _fit_profile(model, rows, tau_index)
    model.mae_kw, model.r2 = _fit_metrics(model, rows, tau_index)
    return model


def _fit_profile(
    model: HeatingModel, rows: list[_TrainingRow], tau_index: int
) -> dict[str, list[float]]:
    """Learn the hour-of-day multiplier, shrunk towards 1 when data is thin."""
    actual: dict[tuple[str, int], float] = {}
    predicted: dict[tuple[str, int], float] = {}
    total_predicted = 0.0
    total_weight = 0.0
    for row in rows:
        base = model.base_kw_at(row.temps_eff[tau_index], row.wind_ms)
        if base <= HEATING_ACTIVE_KW:
            continue
        key = (row.day_type, row.local_hour)
        actual[key] = actual.get(key, 0.0) + row.weight * row.heating_kw
        predicted[key] = predicted.get(key, 0.0) + row.weight * base
        total_predicted += row.weight * base
        total_weight += row.weight
    if total_weight <= 0.0:
        return {}
    prior = PROFILE_PRIOR_HOURS * (total_predicted / total_weight)
    profile: dict[str, list[float]] = {}
    for day_type in DAY_TYPES:
        factors = []
        for hour in range(24):
            key = (day_type, hour)
            ratio = (actual.get(key, 0.0) + prior) / (predicted.get(key, 0.0) + prior)
            factors.append(round(min(PROFILE_MAX, max(PROFILE_MIN, ratio)), 4))
        profile[day_type] = factors
    return profile


def _fit_metrics(
    model: HeatingModel, rows: list[_TrainingRow], tau_index: int
) -> tuple[float | None, float | None]:
    """Return the weighted mean absolute error and R² of the final model."""
    total_weight = sum(row.weight for row in rows)
    if total_weight <= 0.0:
        return None, None
    mean_actual = sum(row.weight * row.heating_kw for row in rows) / total_weight
    abs_error = 0.0
    sse = 0.0
    sst = 0.0
    for row in rows:
        prediction = model.predict_kw(
            row.temps_eff[tau_index], row.wind_ms, row.local_hour, row.day_type
        )
        error = row.heating_kw - prediction
        abs_error += row.weight * abs(error)
        sse += row.weight * error * error
        sst += row.weight * (row.heating_kw - mean_actual) ** 2
    r2 = 1.0 - sse / sst if sst > 0.0 else None
    return round(abs_error / total_weight, 4), (
        round(r2, 4) if r2 is not None else None
    )
