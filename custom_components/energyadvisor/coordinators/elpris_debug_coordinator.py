"""Debug price coordinator backed by elprisetjustnu.se.

Inputs:
    - A callback that accepts price payloads (the price sensor's
      ``async_update_data``), an optional price area and an optional currency.
Outputs:
    - Fetches the prices of one day from elprisetjustnu.se and forwards them in
      the same payload shape the Nord Pool coordinator produces. Only used when
      the ``debug`` option is set, so that any date can be visualised.
    - The slots are moved to the current local day (same time of day), so the
      price sensor and everything built on it treat the chosen day's prices as
      today's. They are moved again when the local day changes.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Coroutine

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

API_URL = (
    "https://www.elprisetjustnu.se/api/v1/prices/"
    "{year}/{month:02d}-{day:02d}_{area}.json"
)
VALID_AREAS = ("SE1", "SE2", "SE3", "SE4")
SUPPORTED_CURRENCIES = ("SEK", "EUR")
DEFAULT_CURRENCY = "SEK"
REQUEST_TIMEOUT_SECONDS = 15

_AREA_IN_ENTITY_ID = re.compile(r"(?<![a-z0-9])(se[1-4])(?![a-z0-9])", re.IGNORECASE)


def area_from_entity_id(entity_id: str | None) -> str | None:
    """Return the price area (SE1-SE4) named in an entity id, if any."""
    if not entity_id:
        return None
    match = _AREA_IN_ENTITY_ID.search(entity_id.split(".", 1)[-1])
    return match.group(1).upper() if match else None


class ElprisDebugCoordinator:
    """Fetch the prices of a selectable day from elprisetjustnu.se.

    Inputs:
        - Home Assistant instance, a payload callback, and optional area and
          currency (``SEK`` or ``EUR``; anything else falls back to ``SEK``).
    Outputs:
        - Today's prices after ``start()``; the prices of any date after
          ``async_select_date()``, published as if they were today's.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        data_update_callback: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
        area: str | None = None,
        currency: str | None = None,
    ) -> None:
        self.hass = hass
        self.data_update_callback = data_update_callback
        self._area = area.upper() if area else None
        wanted_currency = (currency or DEFAULT_CURRENCY).upper()
        self._currency = (
            wanted_currency
            if wanted_currency in SUPPORTED_CURRENCIES
            else DEFAULT_CURRENCY
        )
        self._selected_date: date | None = None
        self._raw: list[dict[str, Any]] = []
        self._cancel_initial_fetch: Callable[[], None] | None = None
        self._cancel_rollover: Callable[[], None] | None = None
        self._is_running = False

    @property
    def selected_date(self) -> date | None:
        """Return the date whose prices are shown as today's."""
        return self._selected_date

    @property
    def area(self) -> str | None:
        """Return the price area used for fetching, if known."""
        return self._area

    def start(self) -> None:
        """Schedule the initial fetch of today's prices."""
        if self._is_running:
            _LOGGER.warning("Elpris debug coordinator already running.")
            return
        self._is_running = True
        _LOGGER.warning(
            "Energy Advisor debug mode is on: prices come from elprisetjustnu.se "
            "(area=%s, currency=%s) instead of Nord Pool.",
            self._area,
            self._currency,
        )
        self._cancel_initial_fetch = async_call_later(
            self.hass, timedelta(milliseconds=100), self._async_initial_fetch
        )

    def stop(self) -> None:
        """Cancel pending work and stop publishing prices."""
        self._is_running = False
        if self._cancel_initial_fetch is not None:
            self._cancel_initial_fetch()
            self._cancel_initial_fetch = None
        if self._cancel_rollover is not None:
            self._cancel_rollover()
            self._cancel_rollover = None

    def _now(self) -> datetime:
        """Return the current time in the Home Assistant time zone."""
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        return datetime.now(tz or dt_util.DEFAULT_TIME_ZONE)

    async def _async_initial_fetch(self, _now: Any = None) -> None:
        self._cancel_initial_fetch = None
        try:
            await self.async_select_date(self._now().date())
        except HomeAssistantError as err:
            _LOGGER.warning("Could not fetch today's debug prices: %s", err)

    async def async_select_date(self, day: date, area: str | None = None) -> int:
        """Fetch and publish the prices of ``day``.

        Inputs:
            - day: the date to show.
            - area: optional area (SE1-SE4); it is remembered for later calls.
        Outputs:
            - The number of price slots published.
        Raises:
            - ServiceValidationError when the area is unknown or no prices
              exist for the date.
            - HomeAssistantError when the download or its parsing fails.
        """
        if not self._is_running:
            raise HomeAssistantError("The debug price coordinator is not running.")
        if area:
            area = area.upper()
            if area not in VALID_AREAS:
                raise ServiceValidationError(
                    f"Unknown price area {area}. Use one of {', '.join(VALID_AREAS)}."
                )
            self._area = area
        if self._area is None:
            raise ServiceValidationError(
                "The price area could not be derived from the Nord Pool price "
                "sensor. Pass 'area' (SE1-SE4) in the action."
            )

        raw = await self._async_fetch_day(day, self._area)
        if not self._is_running:
            return 0
        self._selected_date = day
        self._raw = raw
        published = await self._async_publish()
        _LOGGER.info(
            "Published %s debug price slots of %s in %s as today's prices",
            published,
            day,
            self._area,
        )
        return published

    async def _async_publish(self) -> int:
        """Send the chosen day's prices, moved to today, and arm the rollover."""
        now = self._now()
        raw = self._shift_to_day(self._raw, now.date(), now.tzinfo)
        await self.data_update_callback(
            {"currency": self._currency, "raw": raw, "price_divisor": 1}
        )
        self._schedule_rollover(now)
        return len(raw)

    def _shift_to_day(
        self, raw: list[dict[str, Any]], day: date, tz: Any
    ) -> list[dict[str, Any]]:
        """Move the selected day's slots to ``day``, keeping local clock times."""
        if self._selected_date is None or self._selected_date == day:
            return raw
        offset = timedelta(days=(day - self._selected_date).days)

        def moved(value: str) -> datetime | None:
            parsed = dt_util.parse_datetime(value)
            if parsed is None:
                return None
            return (parsed.astimezone(tz).replace(tzinfo=None) + offset).replace(
                tzinfo=tz
            )

        shifted: list[dict[str, Any]] = []
        seen: set[datetime] = set()
        for entry in raw:
            start, end = moved(entry["start"]), moved(entry["end"])
            # A 25 hour day repeats an hour of clock times; keep the first.
            if start is None or end is None or start in seen:
                continue
            seen.add(start)
            shifted.append(
                {**entry, "start": start.isoformat(), "end": end.isoformat()}
            )
        return shifted

    def _schedule_rollover(self, now: datetime) -> None:
        """Publish the same prices again once the local day changes."""
        if self._cancel_rollover is not None:
            self._cancel_rollover()
        midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        delay = max((midnight - now).total_seconds(), 1.0) + 1.0
        self._cancel_rollover = async_call_later(
            self.hass, timedelta(seconds=delay), self._async_rollover
        )

    async def _async_rollover(self, _now: Any = None) -> None:
        self._cancel_rollover = None
        if not self._is_running or not self._raw:
            return
        try:
            await self._async_publish()
        except Exception:
            _LOGGER.exception("Could not move the debug prices to the new day")

    async def _async_fetch_day(self, day: date, area: str) -> list[dict[str, Any]]:
        """Download one day and convert it to raw currency/MWh entries."""
        url = API_URL.format(year=day.year, month=day.month, day=day.day, area=area)
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            ) as response:
                if response.status == 404:
                    raise ServiceValidationError(
                        f"No prices published for {day} in {area}."
                    )
                response.raise_for_status()
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise HomeAssistantError(f"Could not fetch {url}: {err}") from err

        raw = self._to_raw_entries(data)
        if not raw:
            raise ServiceValidationError(f"No prices found for {day} in {area}.")
        return raw

    def _to_raw_entries(self, data: Any) -> list[dict[str, Any]]:
        """Convert the API response to the payload's currency/MWh entries."""
        if not isinstance(data, list):
            raise HomeAssistantError("Unexpected response from elprisetjustnu.se.")
        price_key = f"{self._currency}_per_kWh"
        raw: list[dict[str, Any]] = []
        for item in data:
            try:
                raw.append(
                    {
                        "start": item["time_start"],
                        "end": item["time_end"],
                        "price": float(item[price_key]) * 1000.0,
                    }
                )
            except (KeyError, TypeError, ValueError):
                _LOGGER.warning("Skipping malformed debug price entry: %s", item)
        return raw
