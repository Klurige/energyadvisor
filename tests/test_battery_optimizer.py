from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.energyadvisor.battery_optimizer import (
    BatteryOptimizationInputs,
    debug_solve_battery_schedule,
    optimize_battery_schedule,
)


TEST_TIMEZONE = ZoneInfo("Europe/Stockholm")


def _make_rates(
    start: datetime,
    costs: list[float],
    credits: list[float],
    slot_hours: float = 1.0,
) -> list[dict[str, object]]:
    """Build price-rate dictionaries for the optimizer."""
    rates: list[dict[str, object]] = []
    current = start
    for cost, credit in zip(costs, credits, strict=True):
        end = current + timedelta(hours=slot_hours)
        rates.append(
            {
                "start": current,
                "end": end,
                "cost": cost,
                "credit": credit,
            }
        )
        current = end
    return rates


def test_optimize_battery_schedule_falls_back_when_disabled() -> None:
    """Optimization disabled should preserve the legacy price schedule."""
    reference_time = datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    inputs = BatteryOptimizationInputs(
        rates=[],
        reference_time=reference_time,
        current_soc_pct=None,
        capacity_kwh=10.0,
        max_charge_power_w=5000.0,
        max_discharge_power_w=5000.0,
        min_soc_pct=5.0,
        max_soc_pct=95.0,
        horizon_hours=48.0,
        optimization_enabled=False,
    )

    result = optimize_battery_schedule(inputs)

    assert result.optimized is False
    assert result.current_mode == "maxuse"
    assert result.schedule[0]["mode"] == "maxuse"


def test_optimize_battery_schedule_charges_then_sells_on_price_spread() -> None:
    """Price spreads should produce charge and sell segments.

    A third, cheap trailing slot is included so the profitable sell slot is
    not the literal horizon-terminal slot: per the terminal SoC value policy
    (docs/battery_charge_mode_optimiser.md section 4.5), selling in the exact
    last slot is objective-neutral by design, which would otherwise make the
    lexicographic tie-break prefer maxuse over a real, non-terminal sell.
    """
    pytest.importorskip("highspy")

    reference_time = datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    rates = _make_rates(
        reference_time,
        costs=[0.10, 0.80, 0.10],
        credits=[0.05, 0.90, 0.05],
    )
    inputs = BatteryOptimizationInputs(
        rates=rates,
        reference_time=reference_time,
        current_soc_pct=50.0,
        capacity_kwh=10.0,
        max_charge_power_w=10000.0,
        max_discharge_power_w=10000.0,
        min_soc_pct=20.0,
        max_soc_pct=80.0,
        horizon_hours=3.0,
        optimization_enabled=True,
    )

    result = optimize_battery_schedule(inputs)

    assert result.optimized is True
    assert result.solver == "HIGHS"
    assert result.current_mode == "charge"
    assert result.current_target_soc_pct == pytest.approx(80.0, abs=0.2)
    assert result.schedule[1]["mode"] == "sell"
    assert result.schedule[0]["from"] == "2026-08-15T12:00"
    assert result.schedule[0]["target_soc"] == pytest.approx(80.0, abs=0.2)
    assert result.schedule[1]["from"] == "2026-08-15T13:00"
    assert result.schedule[1]["target_soc"] == pytest.approx(20.0, abs=0.2)


def test_optimize_battery_schedule_keeps_quarter_hour_slots() -> None:
    """The optimizer should preserve the 15-minute slot granularity."""
    pytest.importorskip("highspy")

    reference_time = datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    rates = _make_rates(
        reference_time,
        costs=[0.10, 0.10, 0.10, 0.10, 0.60, 0.60, 0.60, 0.60],
        credits=[0.05, 0.05, 0.05, 0.05, 0.90, 0.90, 0.90, 0.90],
        slot_hours=0.25,
    )
    inputs = BatteryOptimizationInputs(
        rates=rates,
        reference_time=reference_time,
        current_soc_pct=50.0,
        capacity_kwh=10.0,
        max_charge_power_w=10000.0,
        max_discharge_power_w=10000.0,
        min_soc_pct=20.0,
        max_soc_pct=80.0,
        horizon_hours=2.0,
        optimization_enabled=True,
    )

    result = optimize_battery_schedule(inputs)

    assert result.optimized is True
    assert result.solver == "HIGHS"
    assert len(result.schedule) == 8
    assert [entry["from"] for entry in result.schedule] == [
        "2026-08-15T12:00",
        "2026-08-15T12:15",
        "2026-08-15T12:30",
        "2026-08-15T12:45",
        "2026-08-15T13:00",
        "2026-08-15T13:15",
        "2026-08-15T13:30",
        "2026-08-15T13:45",
    ]


def test_optimize_battery_schedule_keeps_remaining_soc_when_sell_spread_is_flat() -> None:
    """Flat sell/repurchase economics should not trigger a sell cycle."""
    pytest.importorskip("highspy")

    reference_time = datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    rates = _make_rates(
        reference_time,
        costs=[0.50, 0.50, 0.50],
        credits=[0.50, 0.50, 0.50],
    )
    inputs = BatteryOptimizationInputs(
        rates=rates,
        reference_time=reference_time,
        current_soc_pct=80.0,
        capacity_kwh=10.0,
        max_charge_power_w=10000.0,
        max_discharge_power_w=10000.0,
        min_soc_pct=20.0,
        max_soc_pct=80.0,
        horizon_hours=3.0,
        optimization_enabled=True,
    )

    result = optimize_battery_schedule(inputs)

    assert result.optimized is True
    assert result.solver == "HIGHS"
    assert result.current_mode == "maxuse"
    assert result.current_target_soc_pct is None
    assert all(entry["mode"] != "sell" for entry in result.schedule)


def test_optimize_battery_schedule_discharges_on_high_prices() -> None:
    """High current prices with real household load should discharge the battery."""
    pytest.importorskip("highspy")

    reference_time = datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    rates = _make_rates(
        reference_time,
        costs=[0.60, 0.10],
        credits=[0.05, 0.05],
    )
    load_forecasts = [
        {
            "start": reference_time,
            "end": reference_time + timedelta(hours=1),
            "load_kwh": 1.0,
        },
        {
            "start": reference_time + timedelta(hours=1),
            "end": reference_time + timedelta(hours=2),
            "load_kwh": 1.0,
        },
    ]
    inputs = BatteryOptimizationInputs(
        rates=rates,
        load_forecasts=load_forecasts,
        reference_time=reference_time,
        current_soc_pct=80.0,
        capacity_kwh=10.0,
        max_charge_power_w=10000.0,
        max_discharge_power_w=10000.0,
        min_soc_pct=20.0,
        max_soc_pct=80.0,
        horizon_hours=2.0,
        optimization_enabled=True,
    )

    result = optimize_battery_schedule(inputs)

    assert result.optimized is True
    assert result.solver == "HIGHS"
    assert result.current_mode == "discharge"
    # Per the schedule contract, target_soc is null for discharge (only
    # charge and sell carry an end-of-slot target).
    assert result.current_target_soc_pct is None
    assert result.schedule[0]["mode"] == "discharge"
    assert result.schedule[0]["target_soc"] is None


def test_optimize_battery_schedule_reserves_room_for_forecast_solar() -> None:
    """Forecast solar should reduce the target SoC before the solar window."""
    pytest.importorskip("highspy")

    reference_time = datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    rates = _make_rates(
        reference_time,
        costs=[0.10, 0.60],
        credits=[0.05, 0.90],
    )
    solar_forecasts: list[dict[str, object]] = []
    solar_start = reference_time + timedelta(hours=1)
    for index in range(8):
        end = solar_start + timedelta(minutes=15 * (index + 1))
        start = end - timedelta(minutes=15)
        solar_forecasts.append({"start": start, "end": end, "pow": 2.0})

    inputs = BatteryOptimizationInputs(
        rates=rates,
        solar_forecasts=solar_forecasts,
        reference_time=reference_time,
        current_soc_pct=50.0,
        capacity_kwh=10.0,
        max_charge_power_w=10000.0,
        max_discharge_power_w=10000.0,
        min_soc_pct=20.0,
        max_soc_pct=80.0,
        horizon_hours=2.0,
        optimization_enabled=True,
    )

    result = optimize_battery_schedule(inputs)

    assert result.optimized is True
    assert result.solver == "HIGHS"
    assert result.current_mode == "charge"
    assert result.current_target_soc_pct == pytest.approx(60.0, abs=0.2)
    assert "forecast solar" in result.reason
    assert result.schedule[0]["target_soc"] == pytest.approx(60.0, abs=0.2)


def test_optimize_battery_schedule_labels_pv_only_charging_as_maxuse() -> None:
    """Charging purely from PV surplus (no grid import) must be ``maxuse``.

    ``maxuse`` means "maximal use of own-produced energy": it charges the
    battery from solar surplus, discharges to cover any local load, and only
    imports from the grid as a last resort. ``charge`` is reserved for slots
    where the optimizer deliberately funds charging from the grid
    (``b_ch_grid_t > 0``). A slot with abundant PV, no load to speak of, and
    a mediocre local export credit should therefore store the surplus as
    ``maxuse``, not ``charge``, since no grid import is involved.
    """
    pytest.importorskip("highspy")

    reference_time = datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    rates = _make_rates(
        reference_time,
        costs=[0.10, 2.50, 2.50, 2.50],
        credits=[0.05, 1.00, 1.00, 1.00],
        slot_hours=0.25,
    )
    solar_forecasts = []
    load_forecasts = []
    current = reference_time
    for _ in range(4):
        end = current + timedelta(minutes=15)
        solar_forecasts.append({"start": current, "end": end, "pow": 8.0})
        load_forecasts.append({"start": current, "end": end, "load_kwh": 0.05})
        current = end

    inputs = BatteryOptimizationInputs(
        rates=rates,
        solar_forecasts=solar_forecasts,
        load_forecasts=load_forecasts,
        reference_time=reference_time,
        current_soc_pct=50.0,
        capacity_kwh=10.0,
        max_charge_power_w=5000.0,
        max_discharge_power_w=5000.0,
        min_soc_pct=20.0,
        max_soc_pct=80.0,
        horizon_hours=1.0,
        optimization_enabled=True,
    )

    result = optimize_battery_schedule(inputs)
    debug = debug_solve_battery_schedule(inputs)

    assert result.optimized is True
    assert debug is not None
    for index, entry in enumerate(result.schedule):
        if debug.charge_pv_kwh[index] > 0 and debug.charge_grid_kwh[index] == 0:
            assert entry["mode"] == "maxuse"
            assert entry["target_soc"] is None
