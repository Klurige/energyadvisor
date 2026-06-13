"""
This module provides the ElectricityPriceLevel sensor for Home Assistant.

The sensor calculates the current electricity price level (Low, Medium, High)
based on Nord Pool spot prices and user-defined thresholds and fees.
It also provides the calculated cost and credit per kWh. The sensor
updates its state when the underlying Nord Pool sensor for the configured
area updates its price, or when new data is pushed to it.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import datetime

from homeassistant.core import HomeAssistant, Event, State
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.util import dt as dt_util
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)

from ..const import (
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
    CONF_EXCLUDE_FROM_RECORDING,
    parse_unit_of_measurement,
)
from ..util import build_levels_payload_from_rates, level_to_compact

_LOGGER = logging.getLogger(__name__)


class ElectricityPriceLevelsSensor(SensorEntity):
    """
    Representation of an Electricity Price Level sensor.

    This sensor entity monitors electricity prices from Nord Pool,
    calculates costs and credits including various fees and taxes,
    and determines if the current price is 'Low', 'Medium', or 'High'
    based on user-defined thresholds.
    """

    entity_description: SensorEntityDescription
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"rates"})

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device_info: DeviceInfo
    ) -> None:
        """
        Initialize the ElectricityPriceLevel sensor.

        Args:
            hass: The Home Assistant instance.
            entry: The config entry for this sensor.
            device_info: Device information for the entity.
        """
        self._entry = entry

        self._nordpool_prices_sensor = entry.options.get(
            CONF_NORDPOOL_PRICES_SENSOR, ""
        )

        # Initial currency/unit from config (may be empty for migrated entries)
        self._currency = entry.options.get("currency", "") or None
        self._unit = entry.options.get("energy_unit", "") or None
        self._unit_of_measurement = entry.options.get("unit_of_measurement", "") or None
        price_divisor = entry.options.get("price_divisor")
        self._price_divisor = price_divisor if price_divisor is not None else 1

        high_threshold = entry.options.get(CONF_HIGH_THRESHOLD)
        self._high_threshold = (
            high_threshold if high_threshold is not None else 1000000000.0
        )
        low_threshold = entry.options.get(CONF_LOW_THRESHOLD)
        self._low_threshold = (
            low_threshold if low_threshold is not None else -1000000000.0
        )
        supplier_fixed_fee = entry.options.get(CONF_SUPPLIER_FIXED_FEE)
        self._supplier_fixed_fee = (
            supplier_fixed_fee if supplier_fixed_fee is not None else 0.0
        )
        supplier_variable_fee = entry.options.get(CONF_SUPPLIER_VARIABLE_FEE)
        self._supplier_variable_fee = (
            supplier_variable_fee if supplier_variable_fee is not None else 0.0
        )
        supplier_fixed_credit = entry.options.get(CONF_SUPPLIER_FIXED_CREDIT)
        self._supplier_fixed_credit = (
            supplier_fixed_credit if supplier_fixed_credit is not None else 0.0
        )
        supplier_variable_credit = entry.options.get(CONF_SUPPLIER_VARIABLE_CREDIT)
        self._supplier_variable_credit = (
            supplier_variable_credit if supplier_variable_credit is not None else 0.0
        )
        grid_fixed_fee = entry.options.get(CONF_GRID_FIXED_FEE)
        self._grid_fixed_fee = grid_fixed_fee if grid_fixed_fee is not None else 0.0
        grid_variable_fee = entry.options.get(CONF_GRID_VARIABLE_FEE)
        self._grid_variable_fee = (
            grid_variable_fee if grid_variable_fee is not None else 0.0
        )
        grid_fixed_credit = entry.options.get(CONF_GRID_FIXED_CREDIT)
        self._grid_fixed_credit = (
            grid_fixed_credit if grid_fixed_credit is not None else 0.0
        )
        grid_variable_credit = entry.options.get(CONF_GRID_VARIABLE_CREDIT)
        self._grid_variable_credit = (
            grid_variable_credit if grid_variable_credit is not None else 0.0
        )
        grid_energy_tax = entry.options.get(CONF_GRID_ENERGY_TAX)
        self._grid_energy_tax = grid_energy_tax if grid_energy_tax is not None else 0.0
        electricity_vat = entry.options.get(CONF_ELECTRICITY_VAT)
        self._electricity_vat = electricity_vat if electricity_vat is not None else 0.0

        description = SensorEntityDescription(
            key="electricitypricelevels",
            translation_key="electricitypricelevels",
        )
        self.entity_description = description
        self._attr_suggested_object_id = description.key
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

        self._state = 0.0
        self._spot_price = 0.0
        self._cost = 0.0
        self._credit = 0.0
        self._level = "Unknown"
        self._device_class = SensorDeviceClass.MONETARY
        self._icon = "mdi:flash"
        self._rates = []
        self._rank = 0

        self._attr_device_info = device_info
        self._attr_exclude_from_recording = entry.options.get(
            CONF_EXCLUDE_FROM_RECORDING, True
        )

        self._nordpool_trigger_entity_id = self._nordpool_prices_sensor
        self._update_listeners: list[Callable[[], None]] = []

        _LOGGER.debug(
            "ElectricityPriceLevelSensor initialized for prices sensor %s",
            self._nordpool_prices_sensor,
        )

    @property
    def has_rates(self) -> bool:
        """Return whether the sensor currently has rate data."""
        return bool(self._rates)

    def async_add_update_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback that runs when rate data changes."""
        self._update_listeners.append(listener)

        def _remove_listener() -> None:
            if listener in self._update_listeners:
                self._update_listeners.remove(listener)

        return _remove_listener

    def _notify_update_listeners(self) -> None:
        """Notify listeners that the rate data has changed."""
        for listener in tuple(self._update_listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("Error notifying electricity price listeners")

    def build_levels_payload(
        self,
        requested_length: int = 0,
        fill_unknown: bool = False,
        reference_time: datetime.datetime | None = None,
    ) -> dict[str, int | float | str | None]:
        """Build the compact level payload from the internal rate data."""
        local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        now_local = reference_time or datetime.datetime.now(local_tz)
        return build_levels_payload_from_rates(
            self._rates,
            self._low_threshold,
            self._high_threshold,
            now_local,
            requested_length=requested_length,
            fill_unknown=fill_unknown,
        )

    def _update_units_from_nordpool_sensor(self, state=None):
        """Read unit_of_measurement from the Nord Pool sensor and update currency/unit."""
        if state is None and self.hass:
            state = self.hass.states.get(self._nordpool_prices_sensor)
        if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        unit_of_measurement = state.attributes.get("unit_of_measurement", "")
        if not unit_of_measurement:
            return

        parsed_currency, parsed_unit = parse_unit_of_measurement(unit_of_measurement)
        currency_attr = state.attributes.get("currency", "")

        new_currency = parsed_currency or currency_attr or None
        new_unit = parsed_unit or None

        if new_currency and new_currency != self._currency:
            _LOGGER.info(
                f"Updated currency from Nord Pool sensor: {self._currency} -> {new_currency}"
            )
            self._currency = new_currency
        elif not self._currency and new_currency:
            self._currency = new_currency

        if new_unit and new_unit != self._unit:
            _LOGGER.info(
                f"Updated energy unit from Nord Pool sensor: {self._unit} -> {new_unit}"
            )
            self._unit = new_unit
        elif not self._unit and new_unit:
            self._unit = new_unit

        # Always display in kWh regardless of what Nordpool sensor reports,
        # since user-configured fees are all per kWh.
        if self._currency:
            self._unit_of_measurement = f"{self._currency}/kWh"

        # Update price_divisor based on prices_in_cents attribute
        prices_in_cents = state.attributes.get("prices_in_cents")
        if prices_in_cents is not None:
            new_divisor = 100 if prices_in_cents else 1
            if new_divisor != self._price_divisor:
                _LOGGER.info(
                    f"Updated price_divisor from Nord Pool sensor: {self._price_divisor} -> {new_divisor} (prices_in_cents={prices_in_cents})"
                )
                self._price_divisor = new_divisor

    async def async_added_to_hass(self) -> None:
        """
        Run when entity about to be added to Home Assistant.

        This method sets up a listener for state changes of the
        Nord Pool sensor that provides the raw electricity price.
        It also triggers an initial refresh of the sensor state if
        the Nord Pool sensor already has a valid state.
        """
        await super().async_added_to_hass()

        _LOGGER.info(
            f"async_added_to_hass called. Nordpool trigger entity: {self._nordpool_trigger_entity_id}"
        )

        if self._nordpool_trigger_entity_id:
            # Read currency/unit from Nord Pool sensor if available
            initial_trigger_state = self.hass.states.get(
                self._nordpool_trigger_entity_id
            )
            self._update_units_from_nordpool_sensor(initial_trigger_state)

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._nordpool_trigger_entity_id],
                    self._handle_nordpool_trigger_update,
                )
            )
            _LOGGER.info(f"Registered listener for {self._nordpool_trigger_entity_id}")

            if initial_trigger_state and initial_trigger_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                # Only refresh if we have rates data, otherwise wait for coordinator to send it
                if self._rates:
                    _LOGGER.info(
                        f"Initial state of {self._nordpool_trigger_entity_id} is {initial_trigger_state.state}, triggering initial refresh."
                    )
                    await self._refresh_sensor_state()
                else:
                    _LOGGER.info(
                        f"Initial state of {self._nordpool_trigger_entity_id} is {initial_trigger_state.state}, but no rates available yet. Waiting for coordinator data."
                    )
            elif not initial_trigger_state:
                _LOGGER.info(
                    f"Initial state for {self._nordpool_trigger_entity_id} not found. Waiting for coordinator data."
                )

    async def _handle_nordpool_trigger_update(self, event: Event) -> None:
        """
        Handle state changes of the tracked Nordpool sensor.

        This callback is triggered when the Nord Pool sensor, which this
        sensor depends on, updates its state. If the new state is valid,
        this method will refresh the ElectricityPriceLevelSensor's state.

        Args:
            event: The event object containing data about the state change.
                   The new state of the Nord Pool sensor is expected in
                   `event.data.get("new_state")`.
        """
        new_state: State | None = event.data.get("new_state")

        if not new_state or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug(
                f"Tracked Nordpool sensor {self._nordpool_trigger_entity_id} is now {new_state.state if new_state else 'None'}. "
                "Sensor state will not be refreshed by this trigger."
            )
            return

        # Update currency/unit from sensor attributes on every state change
        self._update_units_from_nordpool_sensor(new_state)

        _LOGGER.debug(
            f"Tracked Nordpool sensor {self._nordpool_trigger_entity_id} changed to {new_state.state}. "
            "Refreshing ElectricityPriceLevelSensor state."
        )
        await self._refresh_sensor_state()

    async def _refresh_sensor_state(self) -> None:
        """
        Refreshes the sensor's state based on current rates.

        This method updates the internal state values (_state, _cost, _level, etc.)
        by calling `_update_sensor_state_from_current_rate` and then
        schedules an update to Home Assistant to reflect these changes.
        """
        self._update_sensor_state_from_current_rate()
        self._state = round(self._cost, 5)
        self.async_write_ha_state()
        _LOGGER.info(
            f"Sensor state refreshed: Cost={self._state} {self._currency}/{self._unit}, Level={self._level}, RawSpot={self._spot_price}, Rank={self._rank}"
        )

    @property
    def state(self):
        """
        Return the state of the sensor.

        The state represents the calculated cost of electricity per unit (e.g., kWh),
        rounded to 5 decimal places.
        """
        return self._state

    @property
    def current_credit(self) -> float:
        """Return the current export credit for the active slot."""
        return self._credit

    @property
    def extra_state_attributes(self):
        """
        Return the extra state attributes of the sensor.

        These attributes provide detailed information related to the electricity price,
        including spot price, cost, credit, unit, currency, price level, rank,
        thresholds, and the full list of rates.

        Rates are formatted compactly (local-time ISO strings without timezone,
        3-decimal cost/credit, single-char level) to minimise websocket payload.
        """
        return {
            "spot_price": self._spot_price,
            "cost": self._cost,
            "credit": self._credit,
            "unit": self._unit,
            "currency": self._currency,
            "level": self._level,
            "rank": self._rank,
            "low_threshold": self._low_threshold,
            "high_threshold": self._high_threshold,
            "rates": self._format_rates_compact(),
        }

    def _format_rates_compact(self) -> list[dict]:
        """Format rates list compactly for state attributes."""
        compact = []
        for r in self._rates:
            start = r.get("start")
            if start is None:
                continue
            compact.append(
                {
                    "from": start.strftime("%Y-%m-%dT%H:%M"),
                    "cost": round(r.get("cost", 0), 3),
                    "credit": round(r.get("credit", 0), 3),
                    "level": level_to_compact(r.get("level")),
                    "rank": r.get("rank", "N/A"),
                }
            )
        return compact

    @property
    def unit_of_measurement(self):
        """
        Return the unit of measurement of the sensor.

        Always returns currency/kWh since all fee calculations are per kWh
        and raw Nordpool prices are normalised to kWh internally.
        """
        if self._currency:
            return f"{self._currency}/kWh"
        return self._unit_of_measurement

    @property
    def device_class(self):
        """Return the device class of the sensor."""
        return self._device_class

    @property
    def icon(self):
        """
        Return the icon of the sensor based on the current price level.

        - "mdi:arrow-expand-down" for "Low" level.
        - "mdi:arrow-expand-up" for "High" level.
        - "mdi:arrow-expand-vertical" for "Medium" level.
        - Default icon if level is "Unknown" or not set.
        """
        if self._level == "Low":
            return "mdi:arrow-expand-down"
        if self._level == "High":
            return "mdi:arrow-expand-up"
        if self._level == "Medium":
            return "mdi:arrow-expand-vertical"
        return self._icon

    def _update_sensor_state_from_current_rate(self) -> datetime.datetime | None:
        """
        Update sensor's state attributes from the current hourly rate.

        This method iterates through the stored rates to find the one
        that corresponds to the current time. If a current rate is found,
        it updates the sensor's `_spot_price`, `_cost`, `_credit`, `_level`,
        and `_rank` attributes. It also purges old rates from the `_rates` list.

        Returns:
            The end time of the current rate slot if a current rate is found,
            otherwise None.
        """
        current_rate_details = None
        current_rate_end_time_local = None

        if self._rates:
            try:
                local_tz_str = self.hass.config.time_zone
                local_tz = dt_util.get_time_zone(local_tz_str)
                now_local = datetime.datetime.now(local_tz)
                today_local = now_local.date()

                # Purge old rates
                original_rate_count = len(self._rates)
                self._rates = [
                    rate for rate in self._rates if rate["start"].date() >= today_local
                ]
                purged_count = original_rate_count - len(self._rates)
                if purged_count > 0:
                    _LOGGER.debug(
                        f"Purged {purged_count} old entries from self._rates. Current count: {len(self._rates)}"
                    )

                _LOGGER.debug(
                    "Finding current rate for time: %s in timezone %s",
                    now_local,
                    local_tz_str,
                )

                current_rate_details = next(
                    (
                        rate
                        for rate in self._rates
                        if rate["start"] <= now_local < rate["end"]
                    ),
                    None,
                )

                if current_rate_details:
                    _LOGGER.debug(
                        "Current rate details found: %s", current_rate_details["start"]
                    )
                else:
                    _LOGGER.debug(
                        "No current rate details found for %s in self._rates (%s entries)",
                        now_local,
                        len(self._rates),
                    )

            except Exception as e:
                _LOGGER.error(
                    "Error finding current rate during state update: %s",
                    e,
                    exc_info=True,
                )

        if current_rate_details:
            self._spot_price = current_rate_details["spot_price"]
            self._cost = current_rate_details["cost"]
            self._credit = current_rate_details["credit"]
            self._level = current_rate_details["level"]

            processed_rank = current_rate_details.get("rank")

            if isinstance(processed_rank, (int, float)):
                self._rank = processed_rank
            else:
                self._rank = processed_rank if processed_rank == "N/A" else 0

            current_rate_end_time_local = current_rate_details["end"]
            _LOGGER.debug(
                f"Sensor state updated from current_rate: spot_price={self._spot_price}, cost={self._cost}, level={self._level}, rank={self._rank}. Slot ends at {current_rate_end_time_local}"
            )
        else:
            # Log detailed info about why no rate was found
            if self._rates:
                first_rate_time = self._rates[0].get("start") if self._rates else None
                last_rate_time = self._rates[-1].get("end") if self._rates else None
                _LOGGER.warning(
                    f"No current rate found in self._rates for the current time. "
                    f"Rate count: {len(self._rates)}, "
                    f"Time range: {first_rate_time} to {last_rate_time}. "
                    f"Current time: {datetime.datetime.now(dt_util.get_time_zone(self.hass.config.time_zone))}. "
                    f"Sensor state will be 'Unknown'."
                )
            else:
                _LOGGER.warning(
                    "No rates data available in self._rates. Sensor state will be 'Unknown'."
                )
            self._level = "Unknown"
            self._spot_price = 0.0
            self._cost = 0.0
            self._credit = 0.0
            self._rank = 0

        return current_rate_end_time_local

    async def async_update_data(self, nordpool_data: dict):
        """
        Process new Nordpool data and update sensor state.

        This method is called when new data is available from the Nord Pool
        coordinator. It parses the raw price entries, calculates costs,
        credits, levels, and ranks for each hourly slot, and stores them.
        Finally, it updates the sensor's current state based on this new data.

        Args:
            nordpool_data: A dictionary containing the new Nord Pool data.
                           Expected keys include "currency" and "raw" (a list
                           of price entries).
        """
        _LOGGER.info(
            f"async_update_data CALLED with data. Keys: {list(nordpool_data.keys() if nordpool_data else [])}, Raw entries: {len(nordpool_data.get('raw', [])) if nordpool_data else 0}"
        )
        try:
            # Attempt to read units from the Nord Pool sensor attributes
            self._update_units_from_nordpool_sensor()

            # Use currency from coordinator data (read from currency entity)
            new_currency = nordpool_data.get("currency")
            if new_currency and self._currency != new_currency:
                _LOGGER.info(
                    f"Updated currency from coordinator data: {self._currency} -> {new_currency}"
                )
                self._currency = new_currency

            # Fallback: default to kWh if the Nordpool sensor hasn't reported yet
            if not self._unit:
                self._unit = "kWh"

            # Build unit_of_measurement string — always currency/kWh since all fees
            # are configured per kWh and we normalise raw prices accordingly.
            if self._currency:
                self._unit_of_measurement = f"{self._currency}/kWh"

            self._rates = []
            raw_price_entries = nordpool_data.get("raw", [])

            if raw_price_entries:
                processed_for_ranking = []
                local_tz = dt_util.get_time_zone(self.hass.config.time_zone)

                for entry_data in raw_price_entries:
                    start_local = dt_util.parse_datetime(entry_data["start"])
                    end_local = dt_util.parse_datetime(entry_data["end"])
                    if start_local is None or end_local is None:
                        _LOGGER.warning(
                            f"Skipping entry with unparseable datetime: start={entry_data.get('start')}, end={entry_data.get('end')}"
                        )
                        continue
                    start_local = start_local.astimezone(local_tz)
                    end_local = end_local.astimezone(local_tz)

                    raw_price = entry_data["price"]

                    if raw_price is not None:
                        # get_prices_for_date always returns prices in currency/MWh,
                        # regardless of the Nordpool sensor's display unit_of_measurement
                        # (which HA auto-converts to kWh for display). Divide by 1000
                        # to normalise to currency/kWh for all internal calculations.
                        # prices_in_cents (price_divisor=100) is a separate, orthogonal
                        # concern handled by self._price_divisor.
                        price_kwh = raw_price / 1000.0 / self._price_divisor

                        _LOGGER.debug(
                            f"Processing entry: start={start_local}, end={end_local}, raw_price={raw_price}, price_kwh={price_kwh}, unit={self._unit}, divisor={self._price_divisor}"
                        )
                        processed_for_ranking.append(
                            {"start": start_local, "end": end_local, "value": price_kwh}
                        )

                entries_by_day = {}
                for entry in processed_for_ranking:
                    day = entry["start"].date()
                    if day not in entries_by_day:
                        entries_by_day[day] = []
                    entries_by_day[day].append(entry)

                for day_entries in entries_by_day.values():
                    ranked_day_entries = sorted(day_entries, key=lambda x: x["value"])
                    for entry_to_process in day_entries:
                        self._process_entry(entry_to_process, ranked_day_entries)

                self._rates.sort(key=lambda x: x["start"])

            _LOGGER.info(
                f"Processed {len(self._rates)} rates into self._rates from {len(raw_price_entries)} raw entries"
            )

        except Exception as e:
            _LOGGER.error(
                "Error processing Nordpool data structure: %s. Data: %s",
                e,
                nordpool_data,
                exc_info=True,
            )
            self._level = "Error Processing Data"
            self._cost = 0.0
            self._spot_price = 0.0
            self._state = round(self._cost, 5)
            self.async_write_ha_state()
            return

        self._update_sensor_state_from_current_rate()
        self._state = round(self._cost, 5)
        if self.hass is not None:
            self.async_write_ha_state()
        else:
            _LOGGER.warning(
                "async_write_ha_state called but self.hass is None. Skipping state update."
            )
        _LOGGER.info(
            f"Sensor state updated via async_update_data: Cost={self._state} {self._currency}/{self._unit}, Level={self._level}, RawSpot={self._spot_price}, Rank={self._rank}"
        )
        self._notify_update_listeners()

    async def async_will_remove_from_hass(self) -> None:
        """
        Execute when entity is about to be removed from Home Assistant.

        Performs cleanup tasks, such as logging the removal.
        """
        _LOGGER.debug("Removing ElectricityPriceLevelSensor.")
        await super().async_will_remove_from_hass()

    def _process_entry(self, entry_to_process: dict, daily_ranked_list: list[dict]):
        """
        Process a single price entry to calculate its cost, credit, level, and rank.

        This method takes a single entry (representing an hour's price data)
        and a list of all entries for that day (ranked by price). It calculates
        the final cost and credit including all fees and taxes, determines the
        price level (Low, Medium, High), and calculates a minute rank
        for the price within that day. The processed data is then appended
        to the sensor's `_rates` list.

        Args:
            entry_to_process: A dictionary containing the price entry to process.
                              Expected keys: "start" (datetime), "end" (datetime),
                              "value" (float, spot price in main unit/kWh).
            daily_ranked_list: A list of all price entries for the same day as
                               `entry_to_process`, sorted by price value. This is
                               used to determine the rank.
        """
        start_local = entry_to_process["start"]
        end_local = entry_to_process["end"]
        spot_price_kwh_main_unit = entry_to_process["value"]

        cost, credit = self.calculate_cost_and_credit(spot_price_kwh_main_unit)
        level = self.calculate_level(cost)

        rank_value = "N/A"
        num_entries_today = len(daily_ranked_list)

        try:
            if num_entries_today == 0:
                pass
            else:
                rank_index = next(
                    i
                    for i, ranked_entry in enumerate(daily_ranked_list)
                    if ranked_entry["start"] == start_local
                    and ranked_entry["value"] == spot_price_kwh_main_unit
                )

                if num_entries_today == 1:
                    rank_value = 0
                else:
                    entry_length = 1440 / num_entries_today
                    rank_value = int(rank_index * entry_length)
        except StopIteration:
            _LOGGER.warning(
                f"Could not determine rank for entry starting at {start_local} with value {spot_price_kwh_main_unit}. Appending with rank 'N/A'."
            )
        except Exception as e:
            _LOGGER.error(
                f"Error determining rank for {start_local}: {e}", exc_info=True
            )
            rank_value = "N/A"

        self._rates.append(
            {
                "start": start_local,
                "end": end_local,
                "spot_price": spot_price_kwh_main_unit,
                "cost": cost,
                "credit": credit,
                "level": level,
                "rank": rank_value,
            }
        )

    def calculate_cost_and_credit(
        self, spot_price_main_unit_kwh: float
    ) -> tuple[float, float]:
        """
        Calculate the total cost and credit per kWh based on the spot price and configured fees.

        This method applies various fixed and variable fees (supplier, grid),
        energy tax, and VAT to the spot price to determine the final cost.
        It also calculates the potential credit based on configured credit rates.

        Args:
            spot_price_main_unit_kwh: The raw spot price in the main currency unit per kWh.

        Returns:
            A tuple containing:
                - cost (float): The calculated total cost per kWh, rounded to 5 decimal places.
                - credit (float): The calculated total credit per kWh, rounded to 5 decimal places.
        """
        supplier_fixed_fee = float(self._supplier_fixed_fee)
        supplier_variable_fee_pct = float(self._supplier_variable_fee) / 100
        supplier_fixed_credit = float(self._supplier_fixed_credit)
        supplier_variable_credit_pct = float(self._supplier_variable_credit) / 100
        grid_fixed_fee = float(self._grid_fixed_fee)
        grid_variable_fee_pct = float(self._grid_variable_fee) / 100
        grid_fixed_credit = float(self._grid_fixed_credit)
        grid_variable_credit_pct = float(self._grid_variable_credit) / 100
        grid_energy_tax = float(self._grid_energy_tax)
        electricity_vat_pct = float(self._electricity_vat) / 100

        cost_before_vat = (
            spot_price_main_unit_kwh
            * (1 + supplier_variable_fee_pct + grid_variable_fee_pct)
            + supplier_fixed_fee
            + grid_fixed_fee
            + grid_energy_tax
        )
        cost = cost_before_vat * (1 + electricity_vat_pct)

        credit = (
            spot_price_main_unit_kwh
            * (1 + supplier_variable_credit_pct + grid_variable_credit_pct)
            + supplier_fixed_credit
            + grid_fixed_credit
        )
        return round(cost, 5), round(credit, 5)

    def calculate_level(self, cost: float) -> str:
        """
        Determine the price level (Low, Medium, High) based on the calculated cost.

        Compares the provided cost against the user-configured low and high
        thresholds to categorize the price.

        Args:
            cost: The calculated cost of electricity per kWh.

        Returns:
            A string representing the price level: "Low", "Medium", or "High".
        """
        cost_val = float(cost)
        low = float(self._low_threshold)
        high = float(self._high_threshold)

        _LOGGER.debug(
            f"Calculating level for cost: {cost_val}, low: {low}, high: {high}"
        )
        if cost_val < low:
            return "Low"
        if cost_val > high:
            return "High"
        return "Medium"
