import logging
from datetime import timedelta, date, datetime, time
from typing import Callable, Any, Coroutine

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE

_LOGGER = logging.getLogger(__name__)

class NordpoolDataCoordinator:
    def __init__(self, hass: HomeAssistant, nordpool_config_entry_id: str, data_update_callback: Callable[[dict[str, Any]], Coroutine[Any, Any, None]], currency: str | None = None):
        self.hass = hass
        self.nordpool_config_entry_id = nordpool_config_entry_id
        self.data_update_callback = data_update_callback
        self._task_remover: list[Callable | None] = [None]
        self._current_schedule_state: list[str] = ["INITIALIZING"]
        self._is_running = False

        # Initialize currency from config if provided (may be None for migrated entries)
        self._currency: str | None = currency if currency else None
        self._data_for_current_hass_date: list | None = None # Raw price list for current HASS date
        self._date_of_current_data: date | None = None       # The HASS date for which _data_for_current_hass_date is valid

        self._data_for_next_hass_date: list | None = None    # Raw price list for current HASS date + 1
        self._date_of_next_data: date | None = None          # The HASS date for which _data_for_next_hass_date is valid

    async def _execute_nordpool_call_logic(self, fetch_date: date) -> tuple[str, dict[str, Any] | None]:
        date_to_fetch_str = fetch_date.isoformat()
        service_data = {
            "config_entry": self.nordpool_config_entry_id,
            "date": date_to_fetch_str,
        }
        _LOGGER.info(
            f"Attempting Nordpool call (State: {self._current_schedule_state[0]}) for date: {date_to_fetch_str}"
        )
        try:
            service_response = await self.hass.services.async_call(
                "nordpool",
                "get_prices_for_date",
                service_data,
                blocking=True,
                return_response=True
            )
            _LOGGER.debug(f"Nordpool service call for {date_to_fetch_str} returned: {service_response}")

            if service_response and isinstance(service_response, dict):
                if len(service_response) == 1:
                    area_id = next(iter(service_response))
                    price_data_list = service_response[area_id]

                    if not isinstance(price_data_list, list):
                        _LOGGER.error(
                            f"Nordpool service response for area '{area_id}' is not a list as expected: {type(price_data_list)}"
                        )
                        return "ERROR_BAD_RESPONSE_STRUCTURE", None

                    _LOGGER.info(f"Extracted area '{area_id}' and price data list from service response.")

                    # Determine currency: use configured value, or look up
                    # from the Nord Pool currency entity for this area
                    determined_currency = self._currency
                    if not determined_currency:
                        currency_entity_id = f"sensor.nord_pool_{area_id.lower()}_currency"
                        currency_state_obj = self.hass.states.get(currency_entity_id)
                        if currency_state_obj and currency_state_obj.state not in (None, STATE_UNKNOWN, STATE_UNAVAILABLE):
                            determined_currency = currency_state_obj.state
                            _LOGGER.info(f"Fetched currency '{determined_currency}' from entity '{currency_entity_id}'.")
                        else:
                            _LOGGER.warning(
                                f"Currency entity '{currency_entity_id}' not found or has invalid state "
                                f"({currency_state_obj.state if currency_state_obj else 'None'}). Currency will be None."
                            )

                    final_payload = {
                        "currency": determined_currency,
                        "raw": price_data_list
                    }
                    return "SUCCESS_DATA", final_payload
                else:
                    _LOGGER.error(
                        f"Nordpool service response for {date_to_fetch_str} has unexpected structure "
                        f"(expected 1 area key, got {len(service_response)}): {service_response}"
                    )
                    return "ERROR_BAD_RESPONSE_STRUCTURE", None
            else:
                _LOGGER.warning(f"Nordpool call for {date_to_fetch_str} successful but returned no data or unexpected response: {service_response}")
                return "SUCCESS_NO_DATA", None
        except ServiceValidationError as e:
            if "entry_not_loaded" in str(e).lower() or "not loaded" in str(e).lower() or "did not set up" in str(e).lower():
                _LOGGER.warning(
                    f"Nordpool config entry '{self.nordpool_config_entry_id}' not ready for date {date_to_fetch_str}. Error: {e}"
                )
                return "ERROR_SERVICE_NOT_READY", None
            _LOGGER.error(f"Service validation error for {date_to_fetch_str}: {e}")
            return "ERROR_OTHER", None
        except Exception as e:
            _LOGGER.error(f"Unexpected error calling Nordpool service for {date_to_fetch_str}: {e}", exc_info=True)
            return "ERROR_OTHER", None

    async def _send_updated_data_to_sensor(self, current_hass_date: date) -> None:
        """Combines available data and sends it to the sensor via callback."""
        combined_raw_data = []
        data_sent = False

        _LOGGER.debug(
            f"_send_updated_data_to_sensor called: current_hass_date={current_hass_date}, "
            f"self._data_for_current_hass_date={'present' if self._data_for_current_hass_date else 'None'} (date: {self._date_of_current_data}), "
            f"self._data_for_next_hass_date={'present' if self._data_for_next_hass_date else 'None'} (date: {self._date_of_next_data})"
        )

        if self._data_for_current_hass_date and self._date_of_current_data == current_hass_date:
            combined_raw_data.extend(self._data_for_current_hass_date)
            _LOGGER.debug(f"Including data for {self._date_of_current_data} (current day) in payload. Points: {len(self._data_for_current_hass_date)}")
        elif self._data_for_current_hass_date:
            _LOGGER.warning(f"Not including stale current_day_data (for {self._date_of_current_data}) in payload for HASS date {current_hass_date}.")

        expected_next_day_date = current_hass_date + timedelta(days=1)
        if self._data_for_next_hass_date and self._date_of_next_data == expected_next_day_date:
            combined_raw_data.extend(self._data_for_next_hass_date)
            _LOGGER.debug(f"Including data for {self._date_of_next_data} (next day) in payload. Points: {len(self._data_for_next_hass_date)}")
        elif self._data_for_next_hass_date:
             _LOGGER.warning(f"Not including stale next_day_data (for {self._date_of_next_data}) in payload for HASS date {current_hass_date} (expected next: {expected_next_day_date}).")

        if combined_raw_data:
            if not self._currency:
                _LOGGER.debug("Sending data to sensor without currency (will use configured currency if available).")

            payload_to_send = {
                "currency": self._currency,
                "raw": combined_raw_data
            }
            _LOGGER.info(f"Sending updated combined data to sensor. Currency: {self._currency}, Total points: {len(combined_raw_data)}")
            await self.data_update_callback(payload_to_send)
            data_sent = True
        else:
            _LOGGER.warning(f"No combined_raw_data available to send. Current: {len(self._data_for_current_hass_date) if self._data_for_current_hass_date else 0} items for {self._date_of_current_data}, Next: {len(self._data_for_next_hass_date) if self._data_for_next_hass_date else 0} items for {self._date_of_next_data}")

        if not data_sent and (self._data_for_current_hass_date or self._data_for_next_hass_date):
            _LOGGER.debug(f"Data was available (current: {self._date_of_current_data}, next: {self._date_of_next_data}) but not sent for HASS date {current_hass_date}, likely due to date mismatch.")


    async def _trigger_and_reschedule_nordpool(self, utc_now_from_scheduler: datetime | None = None) -> None:
        if not self._is_running:
            _LOGGER.debug("Coordinator is stopped, not rescheduling.")
            return

        hass_tz = dt_util.get_time_zone(self.hass.config.time_zone)
        hass_now = datetime.now(hass_tz)
        current_hass_date = hass_now.date()

        # 1. Midnight Rollover Logic
        if self._date_of_current_data and self._date_of_current_data < current_hass_date:
            _LOGGER.info(
                f"Midnight rollover: Current HASS date is {current_hass_date}. "
                f"Old current data was for {self._date_of_current_data}, next data for {self._date_of_next_data}."
            )
            self._data_for_current_hass_date = self._data_for_next_hass_date
            self._date_of_current_data = self._date_of_next_data
            # Currency remains as is, from the last successful fetch that set it.

            self._data_for_next_hass_date = None
            self._date_of_next_data = None
            _LOGGER.info(
                f"Rolled over. New current data is for {self._date_of_current_data}, "
                f"next data is now None."
            )

        # 2. Determine what data to fetch
        target_fetch_date: date | None = None
        current_operation_type: str = "IDLE"
        fetch_reason = ""

        if not self._data_for_current_hass_date or self._date_of_current_data != current_hass_date:
            target_fetch_date = current_hass_date
            current_operation_type = "TODAY"
            fetch_reason = f"current data missing or stale (is {self._date_of_current_data})"
        elif self._date_of_current_data == current_hass_date and \
             (not self._data_for_next_hass_date or self._date_of_next_data != (current_hass_date + timedelta(days=1))):
            target_fetch_date = current_hass_date + timedelta(days=1)
            current_operation_type = "TOMORROW"
            fetch_reason = f"next data missing or stale (is {self._date_of_next_data})"
        else:
            _LOGGER.debug(f"Data for today ({self._date_of_current_data}) and tomorrow ({self._date_of_next_data}) appears up-to-date for HASS date {current_hass_date}.")

        call_status = "NOT_ATTEMPTED"
        if target_fetch_date:
            _LOGGER.info(f"Attempting to fetch data for {target_fetch_date} (Operation: {current_operation_type}, Reason: {fetch_reason})")
            call_status, nordpool_day_payload = await self._execute_nordpool_call_logic(target_fetch_date)

            if call_status == "SUCCESS_DATA" and nordpool_day_payload:
                _LOGGER.info(f"Successfully fetched data for {target_fetch_date}. Data points: {len(nordpool_day_payload.get('raw', []))} items. Currency: {nordpool_day_payload.get('currency')}")
                new_raw_data = nordpool_day_payload.get("raw")
                new_currency = nordpool_day_payload.get("currency")

                if new_currency:
                    if self._currency and self._currency != new_currency:
                        _LOGGER.debug(f"Currency changed from {self._currency} to {new_currency}. Using new currency.")
                    self._currency = new_currency
                elif not self._currency:
                     _LOGGER.debug(f"Fetched data for {target_fetch_date} has no currency, using configured currency if available.")

                if current_operation_type == "TODAY":
                    self._data_for_current_hass_date = new_raw_data
                    self._date_of_current_data = target_fetch_date
                    _LOGGER.debug(f"Stored TODAY data: {len(new_raw_data) if new_raw_data else 0} items for {target_fetch_date}")
                elif current_operation_type == "TOMORROW":
                    self._data_for_next_hass_date = new_raw_data
                    self._date_of_next_data = target_fetch_date
                    _LOGGER.debug(f"Stored TOMORROW data: {len(new_raw_data) if new_raw_data else 0} items for {target_fetch_date}")
            elif call_status == "SUCCESS_NO_DATA":
                _LOGGER.warning(f"Fetch for {target_fetch_date} returned SUCCESS but no data payload.")
            elif call_status != "NOT_ATTEMPTED":
                _LOGGER.warning(f"Nordpool call for {target_fetch_date} (Op: {current_operation_type}) failed with status: {call_status}.")

        # 3. Send data to sensor (always attempts to send current valid state)
        await self._send_updated_data_to_sensor(current_hass_date)

        # 4. Rescheduling Logic
        next_delay_seconds: float
        new_log_state_name: str

        # Re-evaluate current state of data *after* the potential fetch in this cycle.
        todays_data_is_now_ok = (self._data_for_current_hass_date and self._date_of_current_data == current_hass_date)
        expected_tomorrows_date = current_hass_date + timedelta(days=1)
        tomorrows_data_is_now_ok = (self._data_for_next_hass_date and self._date_of_next_data == expected_tomorrows_date)

        if not todays_data_is_now_ok:
            # Today's data is still missing/stale. Retry in 15 seconds.
            next_delay_seconds = 15
            new_log_state_name = f"RETRYING_TODAY_DATA_IN_15S (last_fetch_status: {call_status}, op_attempted: {current_operation_type})"
            _LOGGER.warning(f"Today's data for {current_hass_date} is missing or stale ({self._date_of_current_data}). {new_log_state_name}")
        elif not tomorrows_data_is_now_ok:
            # Today's data is OK, but tomorrow's is not.
            _LOGGER.info(f"Today's data for {current_hass_date} is OK. Next day's data ({expected_tomorrows_date}) is missing or stale ({self._date_of_next_data if self._date_of_next_data else 'None'}).")
            time_13h00 = hass_now.replace(hour=13, minute=0, second=0, microsecond=0)

            # If we just successfully fetched TODAY's data, and it's 13:00 or later,
            # schedule an almost immediate attempt for TOMORROW's data.
            if current_operation_type == "TODAY" and call_status == "SUCCESS_DATA" and todays_data_is_now_ok and hass_now >= time_13h00:
                next_delay_seconds = 0.1 # Almost immediate
                new_log_state_name = f"TODAY_OK_IMMEDIATE_FETCH_TOMORROW_POST_13H (last_op_today_status: {call_status})"
            elif hass_now >= time_13h00:
                # Actively retry for tomorrow's data every 120 seconds if it's after 13:00.
                next_delay_seconds = 120
                new_log_state_name = f"RETRYING_TOMORROW_DATA_ACTIVE_IN_120S (last_fetch_status: {call_status}, op_attempted: {current_operation_type})"
            else: # Before 13:00, wait until 13:00 to start fetching tomorrow's data.
                next_run_time_target = time_13h00
                next_delay_seconds = max(0.1, (next_run_time_target - hass_now).total_seconds())
                new_log_state_name = f"WAIT_UNTIL_13:00_FOR_TOMORROW (last_fetch_status: {call_status}, op_attempted: {current_operation_type})"
        else:
            # Both today's and tomorrow's data are OK and up-to-date.
            _LOGGER.info(f"Data for today ({current_hass_date}) and tomorrow ({expected_tomorrows_date}) are up-to-date.")
            next_day_13h00 = (hass_now + timedelta(days=1)).replace(hour=13, minute=0, second=0, microsecond=0)
            next_delay_seconds = max(0.1, (next_day_13h00 - hass_now).total_seconds())
            new_log_state_name = "DAILY_SCHEDULE_NEXT_CHECK_TOMORROW_13:00"

        # Cancel previous task and schedule next one
        if self._task_remover[0]:
            try:
                self._task_remover[0]()
                self._task_remover[0] = None
            except Exception:
                _LOGGER.debug("Exception while trying to cancel previous Nordpool task.", exc_info=True)

        if not self._is_running:
            _LOGGER.info("Coordinator stopped before scheduling next call.")
            return

        if next_delay_seconds <= 0: # Ensure delay is positive, run almost immediately if calculated in past
            _LOGGER.warning(f"Calculated next_delay_seconds was {next_delay_seconds:.2f}. Adjusting to 0.1 second. State: {new_log_state_name}")
            next_delay_seconds = 0.1
        elif next_delay_seconds < 1 and next_delay_seconds != 0.1: # For very small positive delays (but not our special 0.1s), make it 1s
             _LOGGER.warning(f"Calculated next_delay_seconds was {next_delay_seconds:.2f}. Adjusting to 1 second. State: {new_log_state_name}")
             next_delay_seconds = 1

        _LOGGER.info(f"Scheduling next Nordpool call in {next_delay_seconds:.2f} seconds (New State: {new_log_state_name}).")
        self._current_schedule_state[0] = new_log_state_name
        self._task_remover[0] = async_call_later(
            self.hass,
            timedelta(seconds=next_delay_seconds),
            self._trigger_and_reschedule_nordpool
        )

    def start(self) -> None:
        if self._is_running:
            _LOGGER.warning("Coordinator already running.")
            return
        self._is_running = True
        # Reset data on start to ensure fresh fetches, but preserve configured currency
        # Note: self._currency was set during __init__ and should not be reset
        self._data_for_current_hass_date = None
        self._date_of_current_data = None
        self._data_for_next_hass_date = None
        self._date_of_next_data = None

        self._current_schedule_state[0] = "INITIAL_CALL_SCHEDULED"
        _LOGGER.info(f"Nordpool coordinator starting with currency='{self._currency}'. Scheduling initial data fetch. State: {self._current_schedule_state[0]}")

        if self._task_remover[0]:
            try:
                self._task_remover[0]()
            except Exception:
                _LOGGER.debug("Exception during pre-start cancel of existing task.", exc_info=True)
            self._task_remover[0] = None

        self._task_remover[0] = async_call_later(
            self.hass,
            timedelta(milliseconds=100),
            self._trigger_and_reschedule_nordpool
        )

    def stop(self) -> None:
        self._is_running = False
        if self._task_remover[0]:
            _LOGGER.info("Stopping Nordpool data coordinator and cancelling scheduled tasks.")
            try:
                self._task_remover[0]()
                self._task_remover[0] = None
            except Exception as e:
                _LOGGER.warning(f"Error while cancelling Nordpool task on stop: {e}", exc_info=True)
        else:
            _LOGGER.info("Nordpool data coordinator stopped. No active task to cancel.")
        self._current_schedule_state[0] = "STOPPED"

