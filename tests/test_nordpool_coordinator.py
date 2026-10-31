"""Tests for the nordpool coordinator."""

import datetime
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.energyadvisor.coordinators.nordpool_coordinator import (
    NordpoolDataCoordinator,
)


@pytest.fixture
def mock_hass():
    """Mock Home Assistant instance."""
    hass = MagicMock()
    config = MagicMock()
    config.time_zone = "Europe/Stockholm"
    hass.config = config
    hass.states = MagicMock()
    hass.services = AsyncMock()
    return hass


@pytest.fixture
def mock_callback():
    """Mock callback function."""
    return AsyncMock()


@pytest.fixture
def coordinator(mock_hass, mock_callback):
    """Create a NordpoolDataCoordinator instance."""
    coordinator = NordpoolDataCoordinator(
        hass=mock_hass,
        nordpool_config_entry_id="test_config_entry_id",
        data_update_callback=mock_callback,
    )
    return coordinator


@pytest.mark.asyncio
async def test_start_stop(coordinator):
    """Test start and stop functionality."""
    with patch.object(coordinator, "_task_remover", [None]) as mock_task_remover:
        coordinator.start()
        assert coordinator._is_running is True
        assert coordinator._current_schedule_state[0] == "INITIAL_CALL_SCHEDULED"

        # Test stop functionality
        coordinator.stop()
        assert coordinator._is_running is False
        assert coordinator._current_schedule_state[0] == "STOPPED"


@pytest.mark.asyncio
async def test_execute_nordpool_call_success(coordinator, mock_hass):
    """Test successful Nordpool service call."""
    test_date = datetime.date(2025, 8, 9)
    mock_service_response = {
        "SE3": [
            {
                "start": "2025-08-09T00:00:00+02:00",
                "end": "2025-08-09T01:00:00+02:00",
                "value": 10.5,
            },
            {
                "start": "2025-08-09T01:00:00+02:00",
                "end": "2025-08-09T02:00:00+02:00",
                "value": 11.2,
            },
        ]
    }

    mock_hass.services.async_call.return_value = mock_service_response

    # Mock currency entity lookup
    mock_currency_state = MagicMock()
    mock_currency_state.state = "SEK"
    mock_hass.states.get.return_value = mock_currency_state

    status, payload = await coordinator._execute_nordpool_call_logic(test_date)

    assert status == "SUCCESS_DATA"
    assert payload["currency"] == "SEK"
    assert payload["raw"] == mock_service_response["SE3"]

    mock_hass.services.async_call.assert_called_once_with(
        "nordpool",
        "get_prices_for_date",
        {"config_entry": "test_config_entry_id", "date": "2025-08-09"},
        blocking=True,
        return_response=True,
    )


@pytest.mark.asyncio
async def test_execute_nordpool_call_no_currency(coordinator, mock_hass):
    """Test Nordpool service call when currency entity is not available."""
    coordinator._currency = None
    test_date = datetime.date(2025, 8, 9)
    mock_service_response = {
        "SE3": [
            {
                "start": "2025-08-09T00:00:00+02:00",
                "end": "2025-08-09T01:00:00+02:00",
                "value": 10.5,
            },
        ]
    }

    mock_hass.services.async_call.return_value = mock_service_response
    # Currency entity not found
    mock_hass.states.get.return_value = None
    status, payload = await coordinator._execute_nordpool_call_logic(test_date)

    assert status == "SUCCESS_DATA"
    assert payload["currency"] is None
    assert payload["raw"] == mock_service_response["SE3"]


@pytest.mark.asyncio
async def test_execute_nordpool_call_service_not_ready(coordinator, mock_hass):
    """Test Nordpool service call when service is not ready."""
    test_date = datetime.date(2025, 8, 9)
    mock_hass.services.async_call.side_effect = ServiceValidationError(
        "The config entry did not set up."
    )

    status, payload = await coordinator._execute_nordpool_call_logic(test_date)

    assert status == "ERROR_SERVICE_NOT_READY"
    assert payload is None


@pytest.mark.asyncio
async def test_execute_nordpool_call_bad_response(coordinator, mock_hass):
    """Test Nordpool service call with bad response structure."""
    test_date = datetime.date(2025, 8, 9)
    # Response with wrong structure (not a list)
    mock_service_response = {"SE3": "not_a_list"}

    mock_hass.services.async_call.return_value = mock_service_response

    status, payload = await coordinator._execute_nordpool_call_logic(test_date)

    assert status == "ERROR_BAD_RESPONSE_STRUCTURE"
    assert payload is None


@pytest.mark.asyncio
async def test_execute_nordpool_call_no_data_response(coordinator, mock_hass):
    """Test Nordpool service call when the service returns no data."""
    test_date = datetime.date(2025, 8, 9)
    mock_hass.services.async_call.return_value = None

    status, payload = await coordinator._execute_nordpool_call_logic(test_date)

    assert status == "SUCCESS_NO_DATA"
    assert payload is None


@pytest.mark.asyncio
async def test_send_updated_data_to_sensor(coordinator, mock_callback):
    """Test sending updated data to the sensor."""
    current_date = datetime.date(2025, 8, 9)
    coordinator._currency = "EUR"
    coordinator._data_for_current_hass_date = [
        {
            "start": "2025-08-09T00:00:00+02:00",
            "end": "2025-08-09T01:00:00+02:00",
            "value": 10.5,
        },
    ]
    coordinator._date_of_current_data = current_date
    coordinator._data_for_next_hass_date = [
        {
            "start": "2025-08-10T00:00:00+02:00",
            "end": "2025-08-10T01:00:00+02:00",
            "value": 12.1,
        },
    ]
    coordinator._date_of_next_data = current_date + datetime.timedelta(days=1)

    await coordinator._send_updated_data_to_sensor(current_date)

    expected_payload = {
        "currency": "EUR",
        "raw": [
            {
                "start": "2025-08-09T00:00:00+02:00",
                "end": "2025-08-09T01:00:00+02:00",
                "value": 10.5,
            },
            {
                "start": "2025-08-10T00:00:00+02:00",
                "end": "2025-08-10T01:00:00+02:00",
                "value": 12.1,
            },
        ],
    }

    mock_callback.assert_called_once_with(expected_payload)


@pytest.mark.asyncio
async def test_send_updated_data_stale_dates(coordinator, mock_callback):
    """Test sending data with stale dates."""
    current_date = datetime.date(2025, 8, 9)
    coordinator._currency = "EUR"
    # Data from yesterday (stale)
    coordinator._data_for_current_hass_date = [
        {
            "start": "2025-08-08T00:00:00+02:00",
            "end": "2025-08-08T01:00:00+02:00",
            "value": 10.5,
        },
    ]
    coordinator._date_of_current_data = datetime.date(2025, 8, 8)

    # Data for next day is correct
    coordinator._data_for_next_hass_date = [
        {
            "start": "2025-08-10T00:00:00+02:00",
            "end": "2025-08-10T01:00:00+02:00",
            "value": 12.1,
        },
    ]
    coordinator._date_of_next_data = current_date + datetime.timedelta(days=1)

    await coordinator._send_updated_data_to_sensor(current_date)

    # Should only include next day data since current day data is stale
    expected_payload = {
        "currency": "EUR",
        "raw": [
            {
                "start": "2025-08-10T00:00:00+02:00",
                "end": "2025-08-10T01:00:00+02:00",
                "value": 12.1,
            },
        ],
    }

    mock_callback.assert_called_once_with(expected_payload)


@pytest.mark.asyncio
async def test_trigger_rollover_promotes_next_day_data(coordinator):
    """Test rollover behavior inside the trigger method."""
    coordinator._is_running = True
    coordinator._data_for_current_hass_date = ["yesterday_data"]
    coordinator._date_of_current_data = datetime.date(2025, 8, 9)
    coordinator._data_for_next_hass_date = ["today_data"]
    coordinator._date_of_next_data = datetime.date(2025, 8, 10)

    with patch(
        "custom_components.energyadvisor.coordinators.nordpool_coordinator.datetime"
    ) as mock_datetime, patch(
        "custom_components.energyadvisor.coordinators.nordpool_coordinator.async_call_later",
        return_value=lambda: None,
    ), patch.object(
        coordinator,
        "_execute_nordpool_call_logic",
        new=AsyncMock(
            return_value=("SUCCESS_DATA", {"currency": "EUR", "raw": ["tomorrow_data"]})
        ),
    ) as mock_execute, patch.object(
        coordinator,
        "_send_updated_data_to_sensor",
        new=AsyncMock(),
    ):
        mock_datetime.now.return_value = datetime.datetime(2025, 8, 10, 0, 5)
        await coordinator._trigger_and_reschedule_nordpool()

    assert coordinator._data_for_current_hass_date == ["today_data"]
    assert coordinator._date_of_current_data == datetime.date(2025, 8, 10)
    assert coordinator._data_for_next_hass_date == ["tomorrow_data"]
    assert coordinator._date_of_next_data == datetime.date(2025, 8, 11)
    mock_execute.assert_awaited_once_with(datetime.date(2025, 8, 11))


@pytest.mark.asyncio
async def test_trigger_fetches_today_when_missing(coordinator):
    """Test that missing current day data triggers a fetch for today."""
    coordinator._is_running = True
    coordinator._data_for_current_hass_date = None
    coordinator._date_of_current_data = None

    with patch(
        "custom_components.energyadvisor.coordinators.nordpool_coordinator.datetime"
    ) as mock_datetime, patch(
        "custom_components.energyadvisor.coordinators.nordpool_coordinator.async_call_later",
        return_value=lambda: None,
    ), patch.object(
        coordinator,
        "_execute_nordpool_call_logic",
        new=AsyncMock(
            return_value=("SUCCESS_DATA", {"currency": "EUR", "raw": ["today_data"]})
        ),
    ) as mock_execute, patch.object(
        coordinator,
        "_send_updated_data_to_sensor",
        new=AsyncMock(),
    ):
        mock_datetime.now.return_value = datetime.datetime(2025, 8, 10, 10, 0)
        await coordinator._trigger_and_reschedule_nordpool()

    mock_execute.assert_awaited_once_with(datetime.date(2025, 8, 10))
    assert coordinator._data_for_current_hass_date == ["today_data"]
    assert coordinator._date_of_current_data == datetime.date(2025, 8, 10)


@pytest.mark.asyncio
async def test_trigger_fetches_tomorrow_when_missing(coordinator):
    """Test that missing next day data triggers a fetch for tomorrow."""
    coordinator._is_running = True
    coordinator._data_for_current_hass_date = ["today_data"]
    coordinator._date_of_current_data = datetime.date(2025, 8, 10)
    coordinator._data_for_next_hass_date = None
    coordinator._date_of_next_data = None

    with patch(
        "custom_components.energyadvisor.coordinators.nordpool_coordinator.datetime"
    ) as mock_datetime, patch(
        "custom_components.energyadvisor.coordinators.nordpool_coordinator.async_call_later",
        return_value=lambda: None,
    ), patch.object(
        coordinator,
        "_execute_nordpool_call_logic",
        new=AsyncMock(
            return_value=("SUCCESS_DATA", {"currency": "EUR", "raw": ["tomorrow_data"]})
        ),
    ) as mock_execute, patch.object(
        coordinator,
        "_send_updated_data_to_sensor",
        new=AsyncMock(),
    ):
        mock_datetime.now.return_value = datetime.datetime(2025, 8, 10, 14, 0)
        await coordinator._trigger_and_reschedule_nordpool()

    mock_execute.assert_awaited_once_with(datetime.date(2025, 8, 11))
    assert coordinator._data_for_next_hass_date == ["tomorrow_data"]
    assert coordinator._date_of_next_data == datetime.date(2025, 8, 11)


_MODULE = "custom_components.energyadvisor.coordinators.nordpool_coordinator"
_TODAY = datetime.date(2025, 8, 10)
_TOMORROW = datetime.date(2025, 8, 11)


async def _run_cycle(coordinator, now, fetch_result=("SUCCESS_NO_DATA", None)):
    """Run one fetch/reschedule cycle at a fixed local time."""
    new_remover = MagicMock(name="new_remover")
    with patch(f"{_MODULE}.datetime") as mock_datetime, patch(
        f"{_MODULE}.async_call_later", return_value=new_remover
    ) as mock_call_later, patch.object(
        coordinator,
        "_execute_nordpool_call_logic",
        new=AsyncMock(return_value=fetch_result),
    ) as mock_execute:
        mock_datetime.now.return_value = now
        await coordinator._trigger_and_reschedule_nordpool()
    return mock_execute, mock_call_later, new_remover


@pytest.mark.parametrize(
    "error, expected_status",
    [
        (ServiceValidationError("Invalid date requested"), "ERROR_OTHER"),
        (RuntimeError("boom"), "ERROR_OTHER"),
    ],
)
async def test_execute_nordpool_call_errors_return_other(
    coordinator, mock_hass, error, expected_status
):
    """Non-readiness validation errors and unexpected exceptions are contained."""
    mock_hass.services.async_call.side_effect = error

    status, payload = await coordinator._execute_nordpool_call_logic(_TODAY)

    assert status == expected_status
    assert payload is None


async def test_execute_nordpool_call_multiple_areas_rejected(coordinator, mock_hass):
    """A response with more than one area is ambiguous and must be rejected."""
    mock_hass.services.async_call.return_value = {"SE3": [], "SE4": []}

    status, payload = await coordinator._execute_nordpool_call_logic(_TODAY)

    assert status == "ERROR_BAD_RESPONSE_STRUCTURE"
    assert payload is None


@pytest.mark.parametrize(
    "now, current, next_, fetch_result, expected_fetch, expected_delay, expected_raw",
    [
        pytest.param(
            datetime.datetime(2025, 8, 10, 10, 0),
            None,
            None,
            ("ERROR_SERVICE_NOT_READY", None),
            _TODAY,
            15,
            None,
            id="today_fetch_fails_retries_in_15s",
        ),
        pytest.param(
            datetime.datetime(2025, 8, 10, 14, 0),
            None,
            None,
            ("SUCCESS_DATA", {"currency": "SEK", "raw": ["today"]}),
            _TODAY,
            0.1,
            ["today"],
            id="today_fetched_after_13_fetches_tomorrow_immediately",
        ),
        pytest.param(
            datetime.datetime(2025, 8, 10, 14, 0),
            ["today"],
            None,
            ("SUCCESS_NO_DATA", None),
            _TOMORROW,
            120,
            ["today"],
            id="tomorrow_missing_after_13_retries_in_120s",
        ),
        pytest.param(
            datetime.datetime(2025, 8, 10, 10, 30),
            ["today"],
            None,
            ("SUCCESS_NO_DATA", None),
            _TOMORROW,
            2.5 * 3600,
            ["today"],
            id="tomorrow_missing_before_13_waits_until_13",
        ),
        pytest.param(
            datetime.datetime(2025, 8, 10, 12, 59, 59, 500000),
            ["today"],
            None,
            ("SUCCESS_NO_DATA", None),
            _TOMORROW,
            1,
            ["today"],
            id="sub_second_wait_is_rounded_up_to_1s",
        ),
        pytest.param(
            datetime.datetime(2025, 8, 10, 14, 0),
            ["today"],
            ["tomorrow"],
            None,
            None,
            23 * 3600,
            ["today", "tomorrow"],
            id="both_days_present_checks_again_tomorrow_13",
        ),
    ],
)
async def test_trigger_schedules_next_run_and_publishes(
    coordinator,
    mock_callback,
    now,
    current,
    next_,
    fetch_result,
    expected_fetch,
    expected_delay,
    expected_raw,
):
    """The cycle publishes valid data and picks the right retry/next-run delay."""
    coordinator._is_running = True
    if current is not None:
        coordinator._data_for_current_hass_date = current
        coordinator._date_of_current_data = _TODAY
    if next_ is not None:
        coordinator._data_for_next_hass_date = next_
        coordinator._date_of_next_data = _TOMORROW

    mock_execute, mock_call_later, _ = await _run_cycle(coordinator, now, fetch_result)

    if expected_fetch is None:
        mock_execute.assert_not_awaited()
    else:
        mock_execute.assert_awaited_once_with(expected_fetch)

    if expected_raw is None:
        mock_callback.assert_not_awaited()
    else:
        mock_callback.assert_awaited_once()
        assert mock_callback.await_args.args[0]["raw"] == expected_raw

    mock_call_later.assert_called_once()
    assert mock_call_later.call_args.args[1] == datetime.timedelta(
        seconds=expected_delay
    )
    assert (
        mock_call_later.call_args.args[2]
        == coordinator._trigger_and_reschedule_nordpool
    )


@pytest.mark.parametrize("remover_raises", [False, True])
async def test_reschedule_cancels_previous_task(coordinator, remover_raises):
    """Each cycle cancels the pending timer before scheduling a new one."""
    coordinator._is_running = True
    old_remover = MagicMock(
        side_effect=RuntimeError("already fired") if remover_raises else None
    )
    coordinator._task_remover[0] = old_remover

    _, mock_call_later, new_remover = await _run_cycle(
        coordinator, datetime.datetime(2025, 8, 10, 10, 0)
    )

    old_remover.assert_called_once()
    mock_call_later.assert_called_once()

    # Stopping afterwards must cancel the newly scheduled task, not the old one.
    coordinator.stop()
    new_remover.assert_called_once()
    old_remover.assert_called_once()


async def test_stop_cancels_pending_task_and_halts_cycle(coordinator, mock_callback):
    """After stop, the pending timer is cancelled and a late trigger does nothing."""
    remover = MagicMock()
    with patch(f"{_MODULE}.async_call_later", return_value=remover):
        coordinator.start()
    coordinator.stop()

    remover.assert_called_once()

    mock_execute, mock_call_later, _ = await _run_cycle(
        coordinator, datetime.datetime(2025, 8, 10, 10, 0)
    )
    mock_execute.assert_not_awaited()
    mock_call_later.assert_not_called()
    mock_callback.assert_not_awaited()


async def test_stop_during_fetch_prevents_rescheduling(coordinator):
    """Stopping while a fetch is in flight must not leave a new timer behind."""
    coordinator._is_running = True

    async def _fetch_then_stop(_fetch_date):
        coordinator.stop()
        return "SUCCESS_DATA", {"currency": "SEK", "raw": ["today"]}

    with patch(f"{_MODULE}.datetime") as mock_datetime, patch(
        f"{_MODULE}.async_call_later"
    ) as mock_call_later, patch.object(
        coordinator, "_execute_nordpool_call_logic", new=_fetch_then_stop
    ):
        mock_datetime.now.return_value = datetime.datetime(2025, 8, 10, 14, 0)
        await coordinator._trigger_and_reschedule_nordpool()

    mock_call_later.assert_not_called()
