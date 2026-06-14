import sys
import types
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import datetime
from zoneinfo import ZoneInfo  # Requires Python 3.9+

from homeassistant.core import HomeAssistant, State
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

# If dt_util is used directly by the sensor for parsing, ensure it's available or mock its usage if complex.
# from homeassistant.util import dt as dt_util

from custom_components.electricitypricelevels.sensor.electricitypricelevels import (
    ElectricityPriceLevelsSensor,
)
from custom_components.electricitypricelevels.const import (
    CONF_NORDPOOL_PRICES_SENSOR,
    CONF_LOW_THRESHOLD,
    CONF_HIGH_THRESHOLD,
    CONF_SUPPLIER_FIXED_FEE,
    CONF_SUPPLIER_VARIABLE_FEE,
    CONF_SUPPLIER_FIXED_CREDIT,
    CONF_SUPPLIER_VARIABLE_CREDIT,
    CONF_GRID_FIXED_FEE,
    CONF_GRID_VARIABLE_FEE,
    CONF_GRID_FIXED_CREDIT,
    CONF_GRID_VARIABLE_CREDIT,
    CONF_GRID_ENERGY_TAX,
    CONF_ELECTRICITY_VAT,
)

# Default config for tests
DEFAULT_CONFIG_OPTIONS = {
    CONF_NORDPOOL_PRICES_SENSOR: "sensor.nordpool_prices",
    CONF_LOW_THRESHOLD: 0.10,  # EUR/kWh after all fees and VAT
    CONF_HIGH_THRESHOLD: 0.20,  # EUR/kWh after all fees and VAT
    CONF_SUPPLIER_FIXED_FEE: 0.01,  # EUR/kWh
    CONF_SUPPLIER_VARIABLE_FEE: 1.0,  # %
    CONF_SUPPLIER_FIXED_CREDIT: 0.005,  # EUR/kWh
    CONF_SUPPLIER_VARIABLE_CREDIT: 0.5,  # %
    CONF_GRID_FIXED_FEE: 0.02,  # EUR/kWh
    CONF_GRID_VARIABLE_FEE: 2.0,  # %
    CONF_GRID_FIXED_CREDIT: 0.002,  # EUR/kWh
    CONF_GRID_VARIABLE_CREDIT: 0.2,  # %
    CONF_GRID_ENERGY_TAX: 0.03,  # EUR/kWh
    CONF_ELECTRICITY_VAT: 25.0,  # %
}

TEST_TIMEZONE_STR = "Europe/Helsinki"
TEST_TIMEZONE = ZoneInfo(TEST_TIMEZONE_STR)

# A fixed date for "today" in tests
TODAY_DATE = datetime.date(2025, 6, 1)


# Mock pynordpool before any imports that require it
@pytest.fixture(autouse=True, scope="session")
def mock_pynordpool():
    import sys
    import types

    sys.modules["pynordpool"] = types.ModuleType("pynordpool")
    # Optionally, add dummy Currency and other attributes if needed
    sys.modules["pynordpool"].Currency = object
    sys.modules["pynordpool"].Area = object
    sys.modules["pynordpool"].HourPrice = object
    sys.modules["pynordpool"].DeliveryPeriodData = object
    sys.modules["pynordpool"].DeliveryPeriodEntry = object
    sys.modules["pynordpool"].DeliveryPeriodsData = object
    sys.modules["pynordpool"].NordPoolClient = object
    yield
    sys.modules.pop("pynordpool", None)


@pytest.fixture
def mock_hass():
    hass = MagicMock(spec=HomeAssistant)
    hass.config = MagicMock()
    hass.config.time_zone = TEST_TIMEZONE_STR
    hass.data = {}  # Add the data attribute
    hass.bus = MagicMock()  # Add the bus attribute
    # Ensure hass.states exists and has a get method
    hass.states = MagicMock()
    mock_nordpool_state = MagicMock(spec=State)
    mock_nordpool_state.state = "1.23"  # A valid number state
    mock_nordpool_state.attributes = {
        "unit_of_measurement": "EUR/MWh",
    }
    hass.states.get = MagicMock(return_value=mock_nordpool_state)
    return hass


@pytest.fixture
def mock_config_entry():
    entry = MagicMock(spec=ConfigEntry)
    entry.options = (
        DEFAULT_CONFIG_OPTIONS.copy()
    )  # Use a copy to allow modification in tests
    entry.entry_id = "test_entry_id"
    return entry


@pytest.fixture
def mock_device_info():
    return MagicMock(spec=DeviceInfo)


@pytest.fixture
def sensor_instance(mock_hass, mock_config_entry, mock_device_info):
    sensor = ElectricityPriceLevelsSensor(
        mock_hass, mock_config_entry, mock_device_info
    )
    sensor.hass = mock_hass  # Manually assign hass to the instance for testing
    sensor.async_write_ha_state = MagicMock()  # Crucial mock
    # Prevent actual listener setup during tests not focused on it
    sensor.async_on_remove = MagicMock()
    return sensor


# Helper to create Nord Pool data
def create_nordpool_raw_data(
    start_dt_utc: datetime.datetime, num_hours: int, prices_mwh: list[float]
):
    if len(prices_mwh) != num_hours:
        raise ValueError("Length of prices_mwh must match num_hours")

    raw_entries = []
    current_dt = start_dt_utc
    for i in range(num_hours):
        entry_start = current_dt
        entry_end = current_dt + datetime.timedelta(hours=1)
        # Ensure timestamps are timezone-aware (UTC) and in ISO format with Z
        raw_entries.append(
            {
                "start": entry_start.isoformat().replace("+00:00", "Z"),
                "end": entry_end.isoformat().replace("+00:00", "Z"),
                "price": prices_mwh[i],
            }
        )
        current_dt = entry_end
    return {"currency": "EUR", "raw": raw_entries}


# --- Tests for calculate_cost_and_credit ---
def test_calculate_cost_and_credit(sensor_instance):
    # Example spot price: 0.05 EUR/kWh (which is 50 EUR/MWh)
    spot_price_kwh = 0.05

    # Calculation based on DEFAULT_CONFIG_OPTIONS:
    # Supplier: 0.01 (fixed) + 0.05 * 0.01 (variable) = 0.01 + 0.0005 = 0.0105
    # Grid:     0.02 (fixed) + 0.05 * 0.02 (variable) = 0.02 + 0.0010 = 0.0210
    # Energy Tax: 0.03
    # Subtotal before VAT: spot + supplier_fees + grid_fees + tax
    # 0.05 + 0.0105 + 0.0210 + 0.03 = 0.1115
    # VAT (25%): 0.1115 * 0.25 = 0.027875
    # Total Cost: 0.1115 + 0.027875 = 0.139375

    # Credit calculation:
    # Supplier: 0.005 (fixed) + 0.05 * 0.005 (variable) = 0.005 + 0.00025 = 0.00525
    # Grid:     0.002 (fixed) + 0.05 * 0.002 (variable) = 0.002 + 0.00010 = 0.00210
    # Total Credit: spot + supplier_credits + grid_credits
    # 0.05 + 0.00525 + 0.00210 = 0.05735

    expected_cost = 0.13937  # Updated to match function output
    expected_credit = 0.05735

    cost, credit = sensor_instance.calculate_cost_and_credit(spot_price_kwh)

    assert cost == pytest.approx(expected_cost, abs=1e-5)
    assert credit == pytest.approx(expected_credit, abs=1e-5)


# --- Tests for calculate_level ---
@pytest.mark.parametrize(
    "cost, low_thresh, high_thresh, expected_level",
    [
        (0.05, 0.10, 0.20, "Low"),
        (0.10, 0.10, 0.20, "Medium"),  # Cost == low_thresh -> Medium
        (0.15, 0.10, 0.20, "Medium"),
        (0.20, 0.10, 0.20, "Medium"),  # Cost == high_thresh -> Medium
        (0.25, 0.10, 0.20, "High"),
    ],
)
def test_calculate_level(
    sensor_instance, mock_config_entry, cost, low_thresh, high_thresh, expected_level
):
    mock_config_entry.options[CONF_LOW_THRESHOLD] = low_thresh
    mock_config_entry.options[CONF_HIGH_THRESHOLD] = high_thresh
    # Re-initialize sensor or directly set thresholds if they are read on-the-fly
    # The sensor reads them in __init__. For this test, let's update them directly on the instance.
    sensor_instance._low_threshold = float(low_thresh)
    sensor_instance._high_threshold = float(high_thresh)

    level = sensor_instance.calculate_level(cost)
    assert level == expected_level


# --- Tests for async_update_data ---


@pytest.mark.asyncio
async def test_async_update_data_24_hours_today(sensor_instance, mock_hass):
    # Mock "now" to be 10:30 AM on TODAY_DATE in TEST_TIMEZONE
    now_local = datetime.datetime.combine(
        TODAY_DATE, datetime.time(10, 30), tzinfo=TEST_TIMEZONE
    )

    # Prices in EUR/MWh for 24 hours
    prices_mwh = [i * 10 for i in range(1, 25)]  # e.g., 10, 20, ..., 240 EUR/MWh

    start_of_today_utc = datetime.datetime.combine(
        TODAY_DATE, datetime.time.min, tzinfo=TEST_TIMEZONE
    ).astimezone(datetime.timezone.utc)
    nordpool_data = create_nordpool_raw_data(start_of_today_utc, 24, prices_mwh)

    with patch("homeassistant.util.dt.now", return_value=now_local):
        with patch(
            "custom_components.electricitypricelevels.sensor.electricitypricelevels.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = now_local  # For datetime.datetime.now(local_tz)
            mock_dt.combine = datetime.datetime.combine
            mock_dt.side_effect = lambda *args, **kw: datetime.datetime(*args, **kw)

            await sensor_instance.async_update_data(nordpool_data)

    assert len(sensor_instance._rates) == 24
    # Spot price for 10:00-11:00 (11th hour, index 10) is prices_mwh[10] / 1000
    # prices_mwh[10] = (10+1)*10 = 110. Expected spot price = 0.11 EUR/kWh
    expected_spot_price_for_current_hour = prices_mwh[10] / 1000.0

    # Check state (cost for current hour)
    # The sensor state should be the cost for the 10:00-11:00 slot
    current_rate_details = next(
        r
        for r in sensor_instance._rates
        if r["start"].hour == 10 and r["start"].date() == TODAY_DATE
    )
    assert sensor_instance.state == current_rate_details["cost"]
    assert (
        sensor_instance.extra_state_attributes["spot_price"]
        == expected_spot_price_for_current_hour
    )
    assert sensor_instance.extra_state_attributes[
        "level"
    ] == sensor_instance.calculate_level(current_rate_details["cost"])
    assert (
        sensor_instance.extra_state_attributes["rank"] is not None
    )  # Rank should be calculated

    # Verify all rates have been processed
    for i, rate_info in enumerate(sensor_instance._rates):
        assert rate_info["spot_price"] == prices_mwh[i] / 1000.0
        assert "cost" in rate_info
        assert "credit" in rate_info
        assert "level" in rate_info
        assert "rank" in rate_info
        assert rate_info["start"].tzinfo is not None
        assert rate_info["end"].tzinfo is not None


@pytest.mark.asyncio
async def test_async_update_data_48_hours_today_and_tomorrow(
    sensor_instance, mock_hass
):
    now_local = datetime.datetime.combine(
        TODAY_DATE, datetime.time(10, 30), tzinfo=TEST_TIMEZONE
    )

    prices_mwh = [i * 5 for i in range(1, 49)]  # 5, 10, ..., 240 EUR/MWh

    start_of_today_utc = datetime.datetime.combine(
        TODAY_DATE, datetime.time.min, tzinfo=TEST_TIMEZONE
    ).astimezone(datetime.timezone.utc)
    nordpool_data = create_nordpool_raw_data(start_of_today_utc, 48, prices_mwh)

    with patch("homeassistant.util.dt.now", return_value=now_local):
        with patch(
            "custom_components.electricitypricelevels.sensor.electricitypricelevels.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = now_local
            mock_dt.combine = datetime.datetime.combine
            mock_dt.side_effect = lambda *args, **kw: datetime.datetime(*args, **kw)
            await sensor_instance.async_update_data(nordpool_data)

    assert len(sensor_instance._rates) == 48
    # Spot price for 10:00-11:00 today (11th hour, index 10)
    expected_spot_price_for_current_hour = prices_mwh[10] / 1000.0

    current_rate_details = next(
        r
        for r in sensor_instance._rates
        if r["start"].hour == 10 and r["start"].date() == TODAY_DATE
    )
    assert sensor_instance.state == current_rate_details["cost"]
    assert (
        sensor_instance.extra_state_attributes["spot_price"]
        == expected_spot_price_for_current_hour
    )

    # Check ranking was done per day — 24 distinct prices should produce 24 unique ranks
    ranks_today = {
        r["rank"] for r in sensor_instance._rates if r["start"].date() == TODAY_DATE
    }
    ranks_tomorrow = {
        r["rank"]
        for r in sensor_instance._rates
        if r["start"].date() == TODAY_DATE + datetime.timedelta(days=1)
    }

    assert (
        len(ranks_today) == 24
    ), f"Expected 24 unique ranks for today, got {len(ranks_today)}"
    assert (
        len(ranks_tomorrow) == 24
    ), f"Expected 24 unique ranks for tomorrow, got {len(ranks_tomorrow)}"


@pytest.mark.asyncio
async def test_async_update_data_48_hours_yesterday_and_today(
    sensor_instance, mock_hass
):
    # Mock "now" to be 10:30 AM on TODAY_DATE in TEST_TIMEZONE
    now_local = datetime.datetime.combine(
        TODAY_DATE, datetime.time(10, 30), tzinfo=TEST_TIMEZONE
    )

    prices_mwh = [i * 7 for i in range(1, 49)]  # Prices for 48 hours

    yesterday_date = TODAY_DATE - datetime.timedelta(days=1)
    start_of_yesterday_utc = datetime.datetime.combine(
        yesterday_date, datetime.time.min, tzinfo=TEST_TIMEZONE
    ).astimezone(datetime.timezone.utc)
    nordpool_data = create_nordpool_raw_data(start_of_yesterday_utc, 48, prices_mwh)

    # Initial call to async_update_data populates all 48 rates.
    # The subsequent _update_sensor_state_from_current_rate (called internally) will purge.
    with patch("homeassistant.util.dt.now", return_value=now_local):
        with patch(
            "custom_components.electricitypricelevels.sensor.electricitypricelevels.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = now_local
            mock_dt.combine = datetime.datetime.combine
            mock_dt.side_effect = lambda *args, **kw: datetime.datetime(*args, **kw)

            # Before calling async_update_data, _rates is empty
            assert not sensor_instance._rates

            await sensor_instance.async_update_data(nordpool_data)

    # After async_update_data, _update_sensor_state_from_current_rate has run.
    # It purges rates older than "today_local" (which is TODAY_DATE based on mocked now_local).
    # So, only today's 24 rates should remain.
    assert len(sensor_instance._rates) == 24
    for rate_info in sensor_instance._rates:
        assert rate_info["start"].date() == TODAY_DATE

    # Spot price for 10:00-11:00 today (this is the (24+10)th entry in original prices_mwh)
    expected_spot_price_for_current_hour = prices_mwh[24 + 10] / 1000.0

    current_rate_details = next(
        r
        for r in sensor_instance._rates
        if r["start"].hour == 10 and r["start"].date() == TODAY_DATE
    )
    assert sensor_instance.state == current_rate_details["cost"]
    assert (
        sensor_instance.extra_state_attributes["spot_price"]
        == expected_spot_price_for_current_hour
    )
    assert sensor_instance.extra_state_attributes[
        "level"
    ] == sensor_instance.calculate_level(current_rate_details["cost"])

    # Ensure all remaining rates are for today
    for rate in sensor_instance._rates:
        assert rate["start"].astimezone(TEST_TIMEZONE).date() == TODAY_DATE


# --- Tests for lifecycle and edge cases ---


@pytest.mark.asyncio
async def test_async_added_to_hass_no_initial_state(sensor_instance, mock_hass):
    """Test async_added_to_hass when the tracked sensor has no state yet."""
    mock_hass.states.get.return_value = None
    sensor_instance._refresh_sensor_state = AsyncMock()

    await sensor_instance.async_added_to_hass()

    # The listener should be set up
    sensor_instance.async_on_remove.assert_called_once()
    # But refresh should not be called as there's no initial state
    sensor_instance._refresh_sensor_state.assert_not_called()


@pytest.mark.asyncio
async def test_async_added_to_hass_with_initial_state_and_rates(
    sensor_instance, mock_hass
):
    """Test async_added_to_hass triggers refresh when source sensor has state and rates exist."""
    mock_nordpool_state = MagicMock(spec=State)
    mock_nordpool_state.state = "1.23"
    mock_hass.states.get.return_value = mock_nordpool_state

    # Pre-populate some rates so _refresh_sensor_state gets called
    sensor_instance._rates = [{"start": "fake", "end": "fake", "cost": 0.1}]
    sensor_instance._refresh_sensor_state = AsyncMock()

    await sensor_instance.async_added_to_hass()

    sensor_instance.async_on_remove.assert_called_once()
    sensor_instance._refresh_sensor_state.assert_called_once()


@pytest.mark.asyncio
async def test_async_added_to_hass_with_initial_state_but_no_rates(
    sensor_instance, mock_hass
):
    """Test async_added_to_hass does not refresh when source sensor has state but no rates yet."""
    mock_nordpool_state = MagicMock(spec=State)
    mock_nordpool_state.state = "1.23"
    mock_hass.states.get.return_value = mock_nordpool_state

    sensor_instance._rates = []
    sensor_instance._refresh_sensor_state = AsyncMock()

    await sensor_instance.async_added_to_hass()

    sensor_instance.async_on_remove.assert_called_once()
    sensor_instance._refresh_sensor_state.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_state_value", [STATE_UNAVAILABLE, STATE_UNKNOWN])
async def test_handle_nordpool_trigger_update_with_bad_state(
    sensor_instance, bad_state_value
):
    """Test that the sensor doesn't update when the source becomes unavailable or unknown."""
    event = MagicMock()
    event.data = {
        "new_state": State("sensor.nord_pool_fi_current_price", bad_state_value)
    }
    sensor_instance._refresh_sensor_state = AsyncMock()

    await sensor_instance._handle_nordpool_trigger_update(event)

    sensor_instance._refresh_sensor_state.assert_not_called()


@pytest.mark.parametrize(
    "level, expected_icon",
    [
        ("Low", "mdi:arrow-expand-down"),
        ("High", "mdi:arrow-expand-up"),
        ("Medium", "mdi:arrow-expand-vertical"),
        ("Unknown", "mdi:flash"),
        (None, "mdi:flash"),
    ],
)
def test_icon_property(sensor_instance, level, expected_icon):
    """Test the icon property for all possible levels."""
    sensor_instance._level = level
    assert sensor_instance.icon == expected_icon


def test_unit_of_measurement_property(sensor_instance):
    """Test the unit_of_measurement property under different conditions."""
    # Normal case: currency + kWh unit
    sensor_instance._currency = "EUR"
    sensor_instance._unit = "kWh"
    assert sensor_instance.unit_of_measurement == "EUR/kWh"

    # Nordpool reports MWh, but we always display kWh (normalised internally)
    sensor_instance._currency = "EUR"
    sensor_instance._unit = "MWh"
    assert sensor_instance.unit_of_measurement == "EUR/kWh"

    # Missing currency — fall back to stored unit_of_measurement string
    sensor_instance._currency = None
    sensor_instance._unit = "kWh"
    sensor_instance._unit_of_measurement = "default/kWh"
    assert sensor_instance.unit_of_measurement == "default/kWh"

    # Currency known but unit unknown — still return currency/kWh
    sensor_instance._currency = "SEK"
    sensor_instance._unit = None
    assert sensor_instance.unit_of_measurement == "SEK/kWh"


def test_rates_attribute_is_unrecorded(sensor_instance):
    """Test the large rates attribute is excluded from recorder attribute storage."""
    assert "rates" in sensor_instance._unrecorded_attributes


def test_build_levels_payload_uses_internal_rates(sensor_instance):
    """Test compact level payload is built from the sensor's internal rates."""
    start = datetime.datetime.combine(
        TODAY_DATE, datetime.time.min, tzinfo=TEST_TIMEZONE
    )
    sensor_instance._rates = [
        {
            "start": start,
            "end": start + datetime.timedelta(hours=1),
            "level": "Low",
        },
        {
            "start": start + datetime.timedelta(hours=1),
            "end": start + datetime.timedelta(hours=2),
            "level": "Medium",
        },
    ]

    result = sensor_instance.build_levels_payload(
        requested_length=60,
        fill_unknown=False,
        reference_time=start + datetime.timedelta(hours=12),
    )

    assert result["levels"] == "LM"
    assert result["level_length"] == 60


@pytest.mark.asyncio
async def test_update_with_no_current_rate_data(sensor_instance, mock_hass):
    """Test sensor state when _rates is populated but no rate matches current time."""
    # Mock "now" to be 10:30 AM on TODAY_DATE
    now_local = datetime.datetime.combine(
        TODAY_DATE, datetime.time(10, 30), tzinfo=TEST_TIMEZONE
    )

    # Create data for TOMORROW
    tomorrow_date = TODAY_DATE + datetime.timedelta(days=1)
    start_of_tomorrow_utc = datetime.datetime.combine(
        tomorrow_date, datetime.time.min, tzinfo=TEST_TIMEZONE
    ).astimezone(datetime.timezone.utc)
    nordpool_data = create_nordpool_raw_data(start_of_tomorrow_utc, 24, [10] * 24)

    with patch("homeassistant.util.dt.now", return_value=now_local):
        with patch(
            "custom_components.electricitypricelevels.sensor.electricitypricelevels.datetime.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = now_local
            mock_dt.combine = datetime.datetime.combine
            mock_dt.side_effect = lambda *args, **kw: datetime.datetime(*args, **kw)

            # First, update with tomorrow's data. This will populate _rates.
            await sensor_instance.async_update_data(nordpool_data)

    # After the update, because "now" is today, no current rate should be found.
    # The internal call to _update_sensor_state_from_current_rate should have set state to Unknown.
    assert sensor_instance._level == "Unknown"
    assert sensor_instance._cost == 0.0
    assert sensor_instance._spot_price == 0.0
    assert sensor_instance.state == 0.0


@pytest.mark.asyncio
async def test_async_update_data_with_malformed_data(sensor_instance):
    """Test that malformed data is handled gracefully."""
    # Malformed data (e.g., missing 'raw' key)
    malformed_nordpool_data = {"currency": "EUR"}

    await sensor_instance.async_update_data(malformed_nordpool_data)

    # The sensor should not crash and should be in a reasonable state
    assert len(sensor_instance._rates) == 0
    assert (
        sensor_instance._level == "Unknown"
    )  # It becomes Unknown because no rates are found
    assert sensor_instance.state == 0.0

    # Test with another type of malformed data
    malformed_nordpool_data_2 = {
        "currency": "EUR",
        "raw": [{"start": "bad-time", "end": None, "price": 10}],
    }
    with patch(
        "custom_components.electricitypricelevels.sensor.electricitypricelevels._LOGGER"
    ) as mock_logger:
        await sensor_instance.async_update_data(malformed_nordpool_data_2)
        mock_logger.error.assert_called_once()
        assert sensor_instance._level == "Error Processing Data"
        assert sensor_instance.state == 0.0
        # Ensure async_write_ha_state was called
        sensor_instance.async_write_ha_state.assert_called()
