import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.energyadvisor.coordinators import elpris_debug_coordinator
from custom_components.energyadvisor.coordinators.elpris_debug_coordinator import (
    ElprisDebugCoordinator,
    area_from_entity_id,
)

TZ = ZoneInfo("Europe/Stockholm")
DAY = datetime.date(2026, 10, 24)

SAMPLE = [
    {
        "SEK_per_kWh": 0.5,
        "EUR_per_kWh": 0.045,
        "EXR": 11.1,
        "time_start": "2026-10-24T00:00:00+02:00",
        "time_end": "2026-10-24T00:15:00+02:00",
    },
    {
        "SEK_per_kWh": -0.01,
        "EUR_per_kWh": -0.001,
        "EXR": 11.1,
        "time_start": "2026-10-24T00:15:00+02:00",
        "time_end": "2026-10-24T00:30:00+02:00",
    },
]


class _Response:
    def __init__(self, status=200, payload=None, error=None, body=None):
        self.status = status
        self._payload = payload
        self._error = error
        self._body = body

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                MagicMock(), (), status=self.status, message="error"
            )

    async def json(self, content_type=None):
        if self._body is not None:
            return json.loads(self._body)
        return self._payload


def _session(response: _Response) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(return_value=response)
    return session


@pytest.fixture(autouse=True)
def call_later():
    cancel = MagicMock()
    with patch.object(
        elpris_debug_coordinator, "async_call_later", return_value=cancel
    ) as mock:
        mock.cancel = cancel
        yield mock


def _coordinator(
    callback=None, area="SE4", currency=None, running=True, today=DAY
) -> ElprisDebugCoordinator:
    hass = MagicMock()
    hass.config.time_zone = "Europe/Stockholm"
    coordinator = ElprisDebugCoordinator(hass, callback or AsyncMock(), area, currency)
    coordinator._is_running = running
    coordinator._now = lambda: datetime.datetime(
        today.year, today.month, today.day, 12, 0, tzinfo=TZ
    )
    return coordinator


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        ("sensor.nord_pool_se4_current_price", "SE4"),
        ("sensor.nord_pool_SE1_current_price", "SE1"),
        ("sensor.se3_price", "SE3"),
        ("sensor.nordpool_prices", None),
        ("sensor.nord_pool_se5_current_price", None),
        ("sensor.sense4_price", None),
        ("", None),
        (None, None),
    ],
)
def test_area_from_entity_id(entity_id, expected) -> None:
    assert area_from_entity_id(entity_id) == expected


def test_currency_defaults_to_sek_and_falls_back_for_unsupported() -> None:
    assert ElprisDebugCoordinator(MagicMock(), AsyncMock())._currency == "SEK"
    assert ElprisDebugCoordinator(MagicMock(), AsyncMock(), None, "eur")._currency == (
        "EUR"
    )
    assert ElprisDebugCoordinator(MagicMock(), AsyncMock(), None, "NOK")._currency == (
        "SEK"
    )


@pytest.mark.asyncio
async def test_select_date_publishes_currency_per_mwh_payload() -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback)
    session = _session(_Response(payload=SAMPLE))

    with patch.object(
        elpris_debug_coordinator, "async_get_clientsession", return_value=session
    ):
        published = await coordinator.async_select_date(DAY)

    assert published == 2
    assert session.get.call_args.args[0] == (
        "https://www.elprisetjustnu.se/api/v1/prices/2026/10-24_SE4.json"
    )
    callback.assert_awaited_once_with(
        {
            "currency": "SEK",
            "raw": [
                {
                    "start": "2026-10-24T00:00:00+02:00",
                    "end": "2026-10-24T00:15:00+02:00",
                    "price": pytest.approx(500.0),
                },
                {
                    "start": "2026-10-24T00:15:00+02:00",
                    "end": "2026-10-24T00:30:00+02:00",
                    "price": pytest.approx(-10.0),
                },
            ],
            "price_divisor": 1,
        }
    )
    assert coordinator.selected_date == DAY


@pytest.mark.asyncio
async def test_select_date_uses_eur_prices_when_configured() -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback, currency="EUR")

    with patch.object(
        elpris_debug_coordinator,
        "async_get_clientsession",
        return_value=_session(_Response(payload=SAMPLE)),
    ):
        await coordinator.async_select_date(DAY)

    payload = callback.await_args.args[0]
    assert payload["currency"] == "EUR"
    assert [e["price"] for e in payload["raw"]] == [
        pytest.approx(45.0),
        pytest.approx(-1.0),
    ]


@pytest.mark.asyncio
async def test_select_date_zero_pads_month_and_day_and_remembers_area() -> None:
    coordinator = _coordinator(area=None)
    session = _session(_Response(payload=SAMPLE))

    with patch.object(
        elpris_debug_coordinator, "async_get_clientsession", return_value=session
    ):
        await coordinator.async_select_date(datetime.date(2026, 3, 5), "se2")
        await coordinator.async_select_date(datetime.date(2026, 3, 6))

    urls = [call.args[0] for call in session.get.call_args_list]
    assert urls[0].endswith("/2026/03-05_SE2.json")
    assert urls[1].endswith("/2026/03-06_SE2.json")
    assert coordinator.area == "SE2"


@pytest.mark.asyncio
async def test_select_date_without_area_raises_validation_error() -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback, area=None)

    with pytest.raises(ServiceValidationError, match="area"):
        await coordinator.async_select_date(DAY)

    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_select_date_with_unknown_area_raises_validation_error() -> None:
    coordinator = _coordinator()

    with pytest.raises(ServiceValidationError, match="SE9"):
        await coordinator.async_select_date(DAY, "se9")


@pytest.mark.asyncio
async def test_select_date_404_raises_validation_error_and_publishes_nothing() -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback)

    with patch.object(
        elpris_debug_coordinator,
        "async_get_clientsession",
        return_value=_session(_Response(status=404)),
    ):
        with pytest.raises(ServiceValidationError, match="No prices published"):
            await coordinator.async_select_date(DAY)

    callback.assert_not_awaited()
    assert coordinator.selected_date is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _Response(status=500),
        _Response(error=aiohttp.ClientConnectionError("boom")),
        _Response(error=TimeoutError()),
        _Response(body="<html>not json</html>"),
        _Response(payload={"unexpected": "object"}),
    ],
)
async def test_select_date_failures_raise_home_assistant_error(response) -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback)

    with patch.object(
        elpris_debug_coordinator,
        "async_get_clientsession",
        return_value=_session(response),
    ):
        with pytest.raises(HomeAssistantError):
            await coordinator.async_select_date(DAY)

    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_select_date_empty_or_malformed_entries_are_rejected() -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback)
    malformed = [{"time_start": "x"}, {"SEK_per_kWh": "abc"}, "nope"]

    with patch.object(
        elpris_debug_coordinator,
        "async_get_clientsession",
        return_value=_session(_Response(payload=malformed)),
    ):
        with pytest.raises(ServiceValidationError, match="No prices found"):
            await coordinator.async_select_date(DAY)

    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_select_date_skips_malformed_entries_but_keeps_valid_ones() -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback)

    with patch.object(
        elpris_debug_coordinator,
        "async_get_clientsession",
        return_value=_session(_Response(payload=[*SAMPLE, {"SEK_per_kWh": 1}])),
    ):
        assert await coordinator.async_select_date(DAY) == 2


@pytest.mark.asyncio
async def test_select_date_requires_running_coordinator() -> None:
    coordinator = _coordinator(running=False)

    with pytest.raises(HomeAssistantError, match="not running"):
        await coordinator.async_select_date(DAY)


@pytest.mark.asyncio
async def test_select_date_does_not_publish_when_stopped_during_fetch() -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback)

    async def stop_while_fetching(*_args, **_kwargs):
        coordinator.stop()
        return [{"start": "a", "end": "b", "price": 1.0}]

    with patch.object(coordinator, "_async_fetch_day", new=stop_while_fetching):
        assert await coordinator.async_select_date(DAY) == 0

    callback.assert_not_awaited()


def test_start_schedules_initial_fetch_and_stop_cancels_it(call_later) -> None:
    coordinator = ElprisDebugCoordinator(MagicMock(), AsyncMock(), "SE4")

    coordinator.start()
    coordinator.start()

    call_later.assert_called_once()
    assert call_later.call_args.args[2] == coordinator._async_initial_fetch

    coordinator.stop()
    call_later.cancel.assert_called_once_with()
    assert coordinator._is_running is False


@pytest.mark.asyncio
async def test_initial_fetch_selects_today_and_swallows_errors() -> None:
    coordinator = _coordinator()

    with patch.object(
        coordinator,
        "async_select_date",
        new=AsyncMock(side_effect=HomeAssistantError("offline")),
    ) as select_date:
        await coordinator._async_initial_fetch()

    select_date.assert_awaited_once_with(DAY)


@pytest.mark.asyncio
async def test_selected_day_is_published_as_today_keeping_clock_times() -> None:
    callback = AsyncMock()
    today = datetime.date(2026, 11, 2)
    coordinator = _coordinator(callback, today=today)

    with patch.object(
        elpris_debug_coordinator,
        "async_get_clientsession",
        return_value=_session(_Response(payload=SAMPLE)),
    ):
        await coordinator.async_select_date(DAY)

    raw = callback.await_args.args[0]["raw"]
    # The clock changed from +02:00 to +01:00 in between; local times stay.
    assert [(e["start"], e["end"]) for e in raw] == [
        ("2026-11-02T00:00:00+01:00", "2026-11-02T00:15:00+01:00"),
        ("2026-11-02T00:15:00+01:00", "2026-11-02T00:30:00+01:00"),
    ]
    assert [e["price"] for e in raw] == [pytest.approx(500.0), pytest.approx(-10.0)]
    assert coordinator.selected_date == DAY


@pytest.mark.asyncio
async def test_selecting_a_future_day_moves_it_back_to_today() -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback, today=datetime.date(2026, 10, 20))

    with patch.object(
        elpris_debug_coordinator,
        "async_get_clientsession",
        return_value=_session(_Response(payload=SAMPLE)),
    ):
        await coordinator.async_select_date(DAY)

    starts = [e["start"] for e in callback.await_args.args[0]["raw"]]
    assert starts == ["2026-10-20T00:00:00+02:00", "2026-10-20T00:15:00+02:00"]


@pytest.mark.asyncio
async def test_repeated_clock_hour_of_a_25_hour_day_is_kept_once() -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback, today=datetime.date(2026, 11, 2))
    autumn_day = datetime.date(2026, 10, 25)
    first = {
        "SEK_per_kWh": 1.0,
        "time_start": "2026-10-25T02:00:00+02:00",
        "time_end": "2026-10-25T02:15:00+02:00",
    }
    repeated = {
        "SEK_per_kWh": 2.0,
        "time_start": "2026-10-25T02:00:00+01:00",
        "time_end": "2026-10-25T02:15:00+01:00",
    }

    with patch.object(
        elpris_debug_coordinator,
        "async_get_clientsession",
        return_value=_session(_Response(payload=[first, repeated])),
    ):
        await coordinator.async_select_date(autumn_day)

    raw = callback.await_args.args[0]["raw"]
    assert [e["start"] for e in raw] == ["2026-11-02T02:00:00+01:00"]
    assert raw[0]["price"] == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_prices_are_republished_for_the_new_day_at_midnight(call_later) -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback, today=datetime.date(2026, 11, 2))

    with patch.object(
        elpris_debug_coordinator,
        "async_get_clientsession",
        return_value=_session(_Response(payload=SAMPLE)),
    ):
        await coordinator.async_select_date(DAY)

    # 12:00 -> midnight is 12 hours; one extra second guards against early firing.
    call_later.assert_called_once()
    assert call_later.call_args.args[1] == datetime.timedelta(hours=12, seconds=1)
    assert call_later.call_args.args[2] == coordinator._async_rollover

    coordinator._now = lambda: datetime.datetime(2026, 11, 3, 0, 0, 1, tzinfo=TZ)
    await coordinator._async_rollover()

    assert callback.await_count == 2
    starts = [e["start"] for e in callback.await_args.args[0]["raw"]]
    assert starts == ["2026-11-03T00:00:00+01:00", "2026-11-03T00:15:00+01:00"]
    assert coordinator.selected_date == DAY
    assert call_later.call_count == 2


@pytest.mark.asyncio
async def test_rollover_without_data_or_when_stopped_publishes_nothing() -> None:
    callback = AsyncMock()
    coordinator = _coordinator(callback)

    await coordinator._async_rollover()
    assert callback.await_count == 0

    coordinator._raw = [{"start": "x", "end": "y", "price": 1.0}]
    coordinator._is_running = False
    await coordinator._async_rollover()
    assert callback.await_count == 0


@pytest.mark.asyncio
async def test_rollover_errors_are_logged_not_raised() -> None:
    coordinator = _coordinator(AsyncMock(side_effect=RuntimeError("boom")))
    coordinator._raw = [{"start": "x", "end": "y", "price": 1.0}]

    await coordinator._async_rollover()


@pytest.mark.asyncio
async def test_stop_cancels_the_pending_rollover(call_later) -> None:
    coordinator = _coordinator()

    with patch.object(
        elpris_debug_coordinator,
        "async_get_clientsession",
        return_value=_session(_Response(payload=SAMPLE)),
    ):
        await coordinator.async_select_date(DAY)

    coordinator.stop()

    call_later.cancel.assert_called_once_with()
    assert coordinator._cancel_rollover is None
