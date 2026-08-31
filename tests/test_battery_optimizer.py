from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.energyadvisor.battery_optimizer import (
    BatteryOptimizationInputs,
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
    """Price spreads should produce charge and sell segments."""
    pytest.importorskip("highspy")

    reference_time = datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    rates = _make_rates(
        reference_time,
        costs=[0.10, 0.60],
        credits=[0.05, 0.90],
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
    assert result.current_mode == "charge"
    assert result.current_target_soc_pct == pytest.approx(80.0, abs=0.2)
    assert [entry["mode"] for entry in result.schedule] == ["charge", "sell"]
    assert result.schedule[0]["from"] == "2026-08-15T12:00"
    assert result.schedule[0]["target_soc"] == pytest.approx(80.0, abs=0.2)
    assert result.schedule[1]["from"] == "2026-08-15T13:00"
    assert result.schedule[1]["target_soc"] == pytest.approx(20.0, abs=0.2)


def test_optimize_battery_schedule_discharges_on_high_prices() -> None:
    """High current prices should produce a discharge segment."""
    pytest.importorskip("highspy")

    reference_time = datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)
    rates = _make_rates(
        reference_time,
        costs=[0.60, 0.10],
        credits=[0.05, 0.05],
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
        horizon_hours=2.0,
        optimization_enabled=True,
    )

    result = optimize_battery_schedule(inputs)

    assert result.optimized is True
    assert result.solver == "HIGHS"
    assert result.current_mode == "discharge"
    assert result.current_target_soc_pct == pytest.approx(20.0, abs=0.2)
    assert result.schedule[0]["mode"] == "discharge"
    assert result.schedule[0]["target_soc"] == pytest.approx(20.0, abs=0.2)
