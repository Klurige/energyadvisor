"""Off-loop solving, gating and coalescing of the battery charge mode sensor."""

import asyncio
import datetime
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from custom_components.energyadvisor import battery_optimizer
from custom_components.energyadvisor.battery_optimizer import (
    optimize_battery_schedule_if_changed,
)

from .test_batterychargemodesensor_optimizer import _build_sensor

TEST_TIMEZONE = ZoneInfo("Europe/Stockholm")
NOW = datetime.datetime(2026, 8, 15, 12, 0, tzinfo=TEST_TIMEZONE)


@contextmanager
def _clock(now: datetime.datetime):
    with (
        patch(
            "custom_components.energyadvisor.sensor.batterychargemodesensor.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.energyadvisor.sensor.chargemodehelpers.dt_util.now",
            return_value=now,
        ),
    ):
        yield


def _set_soc(sensor, soc_pct: float) -> None:
    sensor.hass.states.get.return_value.state = str(soc_pct)


def _spy_solver():
    return patch.object(
        battery_optimizer,
        "optimize_battery_schedule",
        wraps=battery_optimizer.optimize_battery_schedule,
    )


def _with_background_tasks(sensor):
    """Run background tasks on the test loop and make state writes observable."""
    sensor.hass.async_create_background_task = lambda coro, name: (
        asyncio.get_running_loop().create_task(coro, name=name)
    )
    sensor.async_write_ha_state = MagicMock()
    return sensor


async def test_solve_runs_in_executor() -> None:
    sensor = _build_sensor(NOW)
    with _clock(NOW):
        assert await sensor.async_calculate_battery_mode() is True

    sensor.hass.async_add_executor_job.assert_awaited_once()
    job, inputs, previous = sensor.hass.async_add_executor_job.await_args.args
    assert job is optimize_battery_schedule_if_changed
    assert previous is None
    assert inputs.current_soc_pct == 50.0


async def test_executor_receives_copies_of_price_rates() -> None:
    sensor = _build_sensor(NOW)
    with _clock(NOW):
        await sensor.async_calculate_battery_mode()

    inputs = sensor.hass.async_add_executor_job.await_args.args[1]
    assert len(inputs.rates) == len(sensor._price_sensor._rates)
    for sent, original in zip(inputs.rates, sensor._price_sensor._rates):
        assert sent == original
        assert sent is not original


async def test_unchanged_inputs_do_not_solve_again() -> None:
    sensor = _build_sensor(NOW)
    with _clock(NOW), _spy_solver() as solver:
        assert await sensor.async_calculate_battery_mode() is True
        modes = sensor._modes
        assert await sensor.async_calculate_battery_mode() is False
        assert await sensor.async_calculate_battery_mode() is False

    assert solver.call_count == 1
    assert sensor._modes is modes


async def test_small_soc_change_does_not_solve_again() -> None:
    sensor = _build_sensor(NOW)
    with _clock(NOW), _spy_solver() as solver:
        await sensor.async_calculate_battery_mode()
        _set_soc(sensor, 51)
        assert await sensor.async_calculate_battery_mode() is False
        _set_soc(sensor, 54)
        assert await sensor.async_calculate_battery_mode() is False

    assert solver.call_count == 1


async def test_large_soc_change_solves_again() -> None:
    sensor = _build_sensor(NOW)
    with _clock(NOW), _spy_solver() as solver:
        await sensor.async_calculate_battery_mode()
        _set_soc(sensor, 56)
        assert await sensor.async_calculate_battery_mode() is True

    assert solver.call_count == 2
    assert sensor.extra_state_attributes["current_soc_pct"] == 56.0


async def test_soc_drift_is_measured_from_the_last_solve() -> None:
    """Many 1 pp steps must still add up to a re-solve."""
    sensor = _build_sensor(NOW)
    results = []
    with _clock(NOW), _spy_solver() as solver:
        await sensor.async_calculate_battery_mode()
        for soc in (51, 52, 53, 54, 55):
            _set_soc(sensor, soc)
            results.append(await sensor.async_calculate_battery_mode())

    assert results == [False, False, False, False, True]
    assert solver.call_count == 2


async def test_new_quarter_hour_solves_again() -> None:
    sensor = _build_sensor(NOW)
    with _spy_solver() as solver:
        with _clock(NOW):
            await sensor.async_calculate_battery_mode()
        later = NOW + datetime.timedelta(minutes=15)
        with _clock(later):
            assert await sensor.async_calculate_battery_mode() is True

    assert solver.call_count == 2


async def test_price_change_solves_again() -> None:
    sensor = _build_sensor(NOW)
    with _clock(NOW), _spy_solver() as solver:
        await sensor.async_calculate_battery_mode()
        sensor._price_sensor._rates = [
            {**rate, "cost": rate["cost"] + 0.01}
            for rate in sensor._price_sensor._rates
        ]
        assert await sensor.async_calculate_battery_mode() is True

    assert solver.call_count == 2


async def test_result_for_a_past_slot_is_discarded_and_resolved() -> None:
    sensor = _build_sensor(NOW)
    clock = {"now": NOW}

    def solve_across_the_slot_boundary(func, *args):
        result = func(*args)
        clock["now"] = NOW + datetime.timedelta(minutes=15, seconds=1)
        return result

    sensor.hass.async_add_executor_job.side_effect = solve_across_the_slot_boundary
    modes_before = sensor._modes
    with patch(
        "custom_components.energyadvisor.sensor.batterychargemodesensor.dt_util.now",
        side_effect=lambda: clock["now"],
    ):
        applied = await sensor.async_calculate_battery_mode()

    assert applied is False
    assert sensor._recalc_requested is True
    assert sensor._last_solved_snapshot is None
    assert sensor._modes is modes_before


async def test_requests_are_coalesced_into_a_single_worker() -> None:
    sensor = _with_background_tasks(_build_sensor(NOW))
    release = asyncio.Event()
    calls = 0

    async def slow_calculation() -> bool:
        nonlocal calls
        calls += 1
        await release.wait()
        return True

    sensor.async_calculate_battery_mode = slow_calculation

    sensor._request_recalculation()
    task = sensor._solve_task
    await asyncio.sleep(0)
    for _ in range(5):
        sensor._request_recalculation()
    assert sensor._solve_task is task

    release.set()
    await task

    assert calls == 2
    assert sensor.async_write_ha_state.call_count == 2
    assert sensor._recalc_requested is False


async def test_idle_worker_is_restarted_by_the_next_request() -> None:
    sensor = _with_background_tasks(_build_sensor(NOW))

    async def calculation() -> bool:
        return False

    sensor.async_calculate_battery_mode = calculation

    sensor._request_recalculation()
    first = sensor._solve_task
    await first
    sensor._request_recalculation()
    second = sensor._solve_task
    await second

    assert first is not second
    sensor.async_write_ha_state.assert_not_called()


async def test_worker_survives_a_failed_calculation() -> None:
    sensor = _with_background_tasks(_build_sensor(NOW))
    attempts = []

    async def calculation() -> bool:
        attempts.append(1)
        if len(attempts) == 1:
            sensor._request_recalculation()
            raise RuntimeError("boom")
        return True

    sensor.async_calculate_battery_mode = calculation

    sensor._request_recalculation()
    await sensor._solve_task

    assert len(attempts) == 2
    sensor.async_write_ha_state.assert_called_once()


async def test_removal_cancels_the_worker() -> None:
    sensor = _with_background_tasks(_build_sensor(NOW))
    release = asyncio.Event()

    async def calculation() -> bool:
        await release.wait()
        return True

    sensor.async_calculate_battery_mode = calculation

    sensor._request_recalculation()
    task = sensor._solve_task
    await asyncio.sleep(0)
    await sensor.async_will_remove_from_hass()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert sensor._solve_task is None
    sensor.async_write_ha_state.assert_not_called()


async def test_soc_update_publishes_soc_at_once_and_requests_recalculation() -> None:
    sensor = _build_sensor(NOW)
    sensor.async_write_ha_state = MagicMock()
    sensor._request_recalculation = MagicMock()
    sensor._current_soc_pct = 50.0
    _set_soc(sensor, 55)

    sensor._handle_soc_update(None)

    assert sensor.extra_state_attributes["current_soc_pct"] == 55.0
    sensor.async_write_ha_state.assert_called_once()
    sensor._request_recalculation.assert_called_once()


async def test_source_update_only_requests_recalculation() -> None:
    sensor = _build_sensor(NOW)
    sensor.async_write_ha_state = MagicMock()
    sensor._request_recalculation = MagicMock()

    sensor._handle_source_update()

    sensor._request_recalculation.assert_called_once()
    sensor.async_write_ha_state.assert_not_called()
    sensor.hass.async_add_executor_job.assert_not_called()
