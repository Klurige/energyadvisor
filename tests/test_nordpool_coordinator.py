"""Tests for the nordpool coordinator."""

import datetime
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.energyadvisor.sensor.nordpool_coordinator import (
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
async def test_initialization(coordinator):
    """Test coordinator initialization."""
    assert coordinator.nordpool_config_entry_id == "test_config_entry_id"
    assert coordinator._is_running is False
    assert coordinator._currency is None
    assert coordinator._data_for_current_hass_date is None
    assert coordinator._date_of_current_data is None
    assert coordinator._data_for_next_hass_date is None
    assert coordinator._date_of_next_data is None


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
        "custom_components.energyadvisor.sensor.nordpool_coordinator.datetime"
    ) as mock_datetime, patch(
        "custom_components.energyadvisor.sensor.nordpool_coordinator.async_call_later",
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
        "custom_components.energyadvisor.sensor.nordpool_coordinator.datetime"
    ) as mock_datetime, patch(
        "custom_components.energyadvisor.sensor.nordpool_coordinator.async_call_later",
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
        "custom_components.energyadvisor.sensor.nordpool_coordinator.datetime"
    ) as mock_datetime, patch(
        "custom_components.energyadvisor.sensor.nordpool_coordinator.async_call_later",
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
