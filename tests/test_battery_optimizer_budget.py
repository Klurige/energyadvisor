"""Solver time budget and re-solve gating of the battery optimizer."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

highspy = pytest.importorskip("highspy")

from custom_components.energyadvisor import battery_optimizer  # noqa: E402
from custom_components.energyadvisor.battery_optimizer import (  # noqa: E402
    DEFAULT_SOLVER_TIME_LIMIT_S,
    BatteryOptimizationInputs,
    needs_reoptimization,
    optimize_battery_schedule,
    optimize_battery_schedule_if_changed,
    snapshot_optimization_inputs,
)

TEST_TIMEZONE = ZoneInfo("Europe/Stockholm")
REFERENCE = datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)


def _inputs(
    *,
    reference_time: datetime = REFERENCE,
    slots: int = 12,
    soc_pct: float | None = 50.0,
    with_forecasts: bool = False,
    **overrides,
) -> BatteryOptimizationInputs:
    """A small price-spread problem: cheap, expensive, cheap thirds."""
    rates = []
    for index in range(slots):
        start = REFERENCE + timedelta(minutes=15 * index)
        expensive = slots // 3 <= index < 2 * slots // 3
        rates.append(
            {
                "start": start,
                "end": start + timedelta(minutes=15),
                "cost": 0.80 if expensive else 0.10,
                "credit": 0.90 if expensive else 0.05,
            }
        )
    solar = load = None
    if with_forecasts:
        solar = [
            {
                "start": rate["start"],
                "end": rate["end"],
                "pow": 2.0 if index % 2 else 1.0,
            }
            for index, rate in enumerate(rates)
        ]
        load = [
            {"start": rate["start"], "end": rate["end"], "load_kwh": 0.25}
            for rate in rates
        ]
    values = dict(
        rates=rates,
        reference_time=reference_time,
        current_soc_pct=soc_pct,
        capacity_kwh=10.0,
        max_charge_power_w=5000.0,
        max_discharge_power_w=5000.0,
        min_soc_pct=10.0,
        max_soc_pct=90.0,
        horizon_hours=slots / 4,
        solar_forecasts=solar,
        load_forecasts=load,
    )
    values.update(overrides)
    return BatteryOptimizationInputs(**values)


# --- re-solve gating -------------------------------------------------------


def test_snapshot_does_not_change_as_the_clock_advances_within_a_slot() -> None:
    early = snapshot_optimization_inputs(_inputs())
    late = snapshot_optimization_inputs(
        _inputs(reference_time=REFERENCE + timedelta(minutes=14, seconds=59))
    )

    assert early.slot_start == REFERENCE
    assert late == early


def test_first_solve_is_always_needed() -> None:
    assert needs_reoptimization(None, snapshot_optimization_inputs(_inputs()))


def test_identical_inputs_do_not_need_a_new_solve() -> None:
    previous = snapshot_optimization_inputs(_inputs(with_forecasts=True))
    current = snapshot_optimization_inputs(_inputs(with_forecasts=True))

    assert not needs_reoptimization(previous, current)


@pytest.mark.parametrize(
    ("soc_pct", "expected"),
    [(50.0, False), (52.0, False), (54.9, False), (55.0, True), (45.0, True)],
)
def test_soc_must_move_by_the_threshold(soc_pct: float, expected: bool) -> None:
    previous = snapshot_optimization_inputs(_inputs(soc_pct=50.0))
    current = snapshot_optimization_inputs(_inputs(soc_pct=soc_pct))

    assert needs_reoptimization(previous, current) is expected


def test_soc_threshold_is_configurable() -> None:
    previous = snapshot_optimization_inputs(_inputs(soc_pct=50.0))
    current = snapshot_optimization_inputs(_inputs(soc_pct=51.0))

    assert needs_reoptimization(previous, current, soc_threshold_pct=1.0)


@pytest.mark.parametrize(("before", "after"), [(50.0, None), (None, 50.0)])
def test_soc_becoming_available_or_unavailable_needs_a_new_solve(before, after) -> None:
    previous = snapshot_optimization_inputs(_inputs(soc_pct=before))
    current = snapshot_optimization_inputs(_inputs(soc_pct=after))

    assert needs_reoptimization(previous, current)


def test_new_quarter_hour_needs_a_new_solve() -> None:
    previous = snapshot_optimization_inputs(_inputs())
    current = snapshot_optimization_inputs(
        _inputs(reference_time=REFERENCE + timedelta(minutes=15))
    )

    assert needs_reoptimization(previous, current)


def test_changed_price_needs_a_new_solve() -> None:
    changed = _inputs()
    changed.rates[3] = {**changed.rates[3], "cost": changed.rates[3]["cost"] + 0.01}

    assert needs_reoptimization(
        snapshot_optimization_inputs(_inputs()),
        snapshot_optimization_inputs(changed),
    )


def test_changed_battery_settings_need_a_new_solve() -> None:
    assert needs_reoptimization(
        snapshot_optimization_inputs(_inputs()),
        snapshot_optimization_inputs(_inputs(capacity_kwh=12.0)),
    )


def _with_solar_power(inputs: BatteryOptimizationInputs, index: int, power_kw: float):
    inputs.solar_forecasts[index] = {**inputs.solar_forecasts[index], "pow": power_kw}
    return inputs


@pytest.mark.parametrize(("delta_kw", "expected"), [(0.2, False), (0.3, True)])
def test_forecast_must_move_by_the_threshold(delta_kw: float, expected: bool) -> None:
    previous = _inputs(with_forecasts=True)
    changed = _inputs(with_forecasts=True)
    _with_solar_power(changed, 5, changed.solar_forecasts[5]["pow"] + delta_kw)

    assert (
        needs_reoptimization(
            snapshot_optimization_inputs(previous),
            snapshot_optimization_inputs(changed),
        )
        is expected
    )


def test_load_forecast_change_is_detected() -> None:
    previous = _inputs(with_forecasts=True)
    changed = _inputs(with_forecasts=True)
    # 0.25 kWh -> 0.5 kWh in a 15-minute slot is +1 kW.
    changed.load_forecasts[4] = {**changed.load_forecasts[4], "load_kwh": 0.5}

    assert needs_reoptimization(
        snapshot_optimization_inputs(previous),
        snapshot_optimization_inputs(changed),
    )


def test_forecast_appearing_or_disappearing_is_detected() -> None:
    assert needs_reoptimization(
        snapshot_optimization_inputs(_inputs()),
        snapshot_optimization_inputs(_inputs(with_forecasts=True)),
    )
    assert needs_reoptimization(
        snapshot_optimization_inputs(_inputs(with_forecasts=True)),
        snapshot_optimization_inputs(_inputs()),
    )


def test_optimize_if_changed_skips_the_solver_when_nothing_changed() -> None:
    inputs = _inputs()
    snapshot, first = optimize_battery_schedule_if_changed(inputs, None)
    assert first is not None

    with patch.object(battery_optimizer, "optimize_battery_schedule") as solver:
        again, second = optimize_battery_schedule_if_changed(inputs, snapshot)

    assert second is None
    assert again == snapshot
    solver.assert_not_called()


# --- solver time budget ----------------------------------------------------


class _RecordingHighs:
    """Delegates to a real HiGHS model and records the options of every run."""

    def __init__(self, *, model_status=None, primal_solution_status=None) -> None:
        self._real = highspy.Highs()
        self._options: dict[str, object] = {}
        self._model_status = model_status
        self._primal_solution_status = primal_solution_status
        self.runs: list[dict[str, object]] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def setOptionValue(self, name, value):
        self._options[name] = value
        return self._real.setOptionValue(name, value)

    def run(self):
        self.runs.append(dict(self._options))
        if self._primal_solution_status == 0:
            return highspy.HighsStatus.kWarning
        status = self._real.run()
        if self._model_status is not None:
            return highspy.HighsStatus.kWarning
        return status

    def getModelStatus(self):
        if self._model_status is not None:
            return self._model_status
        return self._real.getModelStatus()

    def getInfo(self):
        if self._primal_solution_status == 0:
            return SimpleNamespace(primal_solution_status=0)
        return self._real.getInfo()


def _solve_with(highs: _RecordingHighs, inputs: BatteryOptimizationInputs):
    with patch.object(highspy, "Highs", lambda: highs):
        return optimize_battery_schedule(inputs)


def test_solver_options_use_one_thread_and_a_sliced_time_budget() -> None:
    highs = _RecordingHighs()

    result = _solve_with(highs, _inputs(with_forecasts=True))

    assert result.optimized is True
    assert "time limit" not in result.reason
    # Economic pass is exact (it defines the lexicographic pins); the headroom,
    # maxuse and flow tie-breaks may stop at a 0.1 % gap.
    assert [run["mip_rel_gap"] for run in highs.runs] == [0.0, 1e-3, 1e-3, 1e-3]
    assert all(run["threads"] == 1 for run in highs.runs)
    limits = [run["time_limit"] for run in highs.runs]
    assert all(0 < limit <= DEFAULT_SOLVER_TIME_LIMIT_S for limit in limits)
    # Tie-breaks split what is left (a third, then a half, then the rest) so an
    # early pass can never starve a later one.
    assert limits[1] <= DEFAULT_SOLVER_TIME_LIMIT_S / 3
    assert limits[2] <= DEFAULT_SOLVER_TIME_LIMIT_S / 2


def test_custom_time_limit_bounds_every_pass() -> None:
    highs = _RecordingHighs()

    _solve_with(highs, _inputs(solver_time_limit_s=4.0))

    assert all(0 < run["time_limit"] <= 4.0 for run in highs.runs)


def test_spent_budget_skips_tie_breaks_but_keeps_the_economic_schedule() -> None:
    exact = optimize_battery_schedule(_inputs())
    clock = MagicMock()
    # The deadline is taken at the first reading; every later one is past it.
    clock.monotonic.side_effect = [0.0] + [1_000.0] * 20

    with patch.object(battery_optimizer, "time", clock):
        limited = optimize_battery_schedule(_inputs(solver_time_limit_s=5.0))

    assert limited.optimized is True
    assert "solver time limit of 5s reached" in limited.reason
    assert limited.objective_value == pytest.approx(exact.objective_value)
    assert limited.reason.startswith("Optimized 3h price schedule with HiGHS")
    assert limited.reason.endswith(").")


def test_time_limited_incumbent_is_used_and_stops_refinement() -> None:
    highs = _RecordingHighs(
        model_status=highspy.HighsModelStatus.kTimeLimit,
        primal_solution_status=highspy.kSolutionStatusFeasible,
    )

    result = _solve_with(highs, _inputs())

    assert result.optimized is True
    assert "time limit" in result.reason
    assert len(highs.runs) == 1


def test_time_limit_without_a_feasible_solution_falls_back() -> None:
    highs = _RecordingHighs(
        model_status=highspy.HighsModelStatus.kTimeLimit,
        primal_solution_status=0,
    )

    result = _solve_with(highs, _inputs())

    assert result.optimized is False
    assert result.current_mode == "maxuse"
    assert "failed to solve" in result.reason
