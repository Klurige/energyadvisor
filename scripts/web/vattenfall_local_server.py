#!/usr/bin/env python3
"""Serve local Energy Advisor web tooling with Vattenfall-backed data."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

WEB_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_ROOT.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

VATTENFALL_URL = (
    "https://selfserviceapi.www.vattenfall.se/" "elements/nordpool/aggregatedspotprices"
)
DEFAULT_COLLECTION_HOURS = 48
SUPPORTED_COLLECTION_HOURS = {24, 48}
LOCAL_TIMEZONE = ZoneInfo("Europe/Stockholm")
NORDPOOL_AREA = "SE4"
NORDPOOL_CURRENCY = "SEK"
DEFAULT_LOG_LEVEL = "INFO"

_LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _configure_logging(level_name: str) -> None:
    """Configure process-wide logging for the local web harness."""
    log_level = level_name.upper()
    if log_level not in _LOG_LEVEL_CHOICES:
        raise ValueError(
            f"Unsupported log level '{level_name}'. Expected one of: "
            + ", ".join(_LOG_LEVEL_CHOICES)
        )

    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Surface Energy Advisor internals while avoiding excessive third-party noise.
    logging.getLogger("custom_components.energyadvisor").setLevel(
        getattr(logging, log_level)
    )


class UpstreamRequestError(Exception):
    """Raised when the upstream Vattenfall API request fails."""


@dataclass
class _StubState:
    """Minimal Home Assistant State stand-in used by the local runtime."""

    state: str
    attributes: dict[str, Any]


class _StubStates:
    """Minimal Home Assistant states registry used by the local runtime."""

    def __init__(self, state_map: dict[str, _StubState]):
        self._state_map = state_map

    def get(self, entity_id: str):
        return self._state_map.get(entity_id)


class _StubServices:
    """Minimal Home Assistant services facade for the Nord Pool coordinator."""

    def __init__(
        self,
        async_call_handler: Callable[
            [str, str, dict[str, Any], bool, bool], Awaitable[dict[str, Any] | None]
        ],
    ):
        self._async_call_handler = async_call_handler

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        blocking: bool = True,
        return_response: bool = True,
    ) -> dict[str, Any] | None:
        return await self._async_call_handler(
            domain, service, service_data, blocking, return_response
        )


class _StubHass:
    """Small subset of Home Assistant used by the coordinator/sensors."""

    def __init__(
        self,
        time_zone: str,
        service_handler: Callable[
            [str, str, dict[str, Any], bool, bool], Awaitable[dict[str, Any] | None]
        ],
        state_map: dict[str, _StubState],
    ) -> None:
        self.config = SimpleNamespace(time_zone=time_zone)
        self.states = _StubStates(state_map)
        self.services = _StubServices(service_handler)
        self._tasks: list[asyncio.Task] = []

    def async_create_task(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    def async_create_background_task(self, coro, _name: str = ""):
        return self.async_create_task(coro)

    async def drain_background_tasks(self) -> None:
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def start_epoch_for_day(day: date) -> int:
    """Return UTC midnight for a date as Unix epoch seconds."""
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def parse_requested_hours(raw_hours: str | None) -> int:
    """Validate and parse query-hours (24 or 48)."""
    if raw_hours in (None, ""):
        return DEFAULT_COLLECTION_HOURS

    try:
        requested_hours = int(raw_hours)
    except ValueError as error:
        raise ValueError("Invalid hours. Expected hours=24 or hours=48.") from error

    if requested_hours not in SUPPORTED_COLLECTION_HOURS:
        raise ValueError("Invalid hours. Expected hours=24 or hours=48.")

    return requested_hours


def parse_selected_day(raw_date: str | None) -> date:
    """Validate and parse query-date (YYYY-MM-DD)."""
    if not raw_date:
        raise ValueError("Missing required query parameter: date=YYYY-MM-DD")

    try:
        return date.fromisoformat(raw_date)
    except ValueError as error:
        raise ValueError("Invalid date format. Expected YYYY-MM-DD.") from error


def _fetch_vattenfall_payload(delivery_start: int, delivery_end: int) -> dict[str, Any]:
    """Fetch aggregated spot prices from Vattenfall for a given epoch range."""
    upstream_query = urlencode(
        {
            "deliveryAreas": NORDPOOL_AREA,
            "currency": NORDPOOL_CURRENCY,
            "deliveryStart": delivery_start,
            "deliveryEnd": delivery_end,
            "resolution": "15mins",
            "timezone": "CET",
        }
    )
    upstream_url = f"{VATTENFALL_URL}?{upstream_query}"
    request = Request(
        upstream_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            upstream_payload = response.read()
    except HTTPError as error:
        raise UpstreamRequestError(
            f"Upstream request failed: HTTP {error.code} {error.reason}"
        ) from error
    except URLError as error:
        raise UpstreamRequestError(
            f"Could not reach upstream API: {error.reason}"
        ) from error

    try:
        return json.loads(upstream_payload.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise UpstreamRequestError("Upstream API did not return valid JSON") from error


def _vattenfall_prices_to_nordpool_rows(
    vattenfall_payload: dict[str, Any],
) -> list[dict]:
    """Convert Vattenfall rows to Nord Pool service shape used by PriceSensor."""
    converted: list[dict] = []
    for row in vattenfall_payload.get("prices", []):
        measurement = row.get("measurement") if isinstance(row, dict) else None
        value = measurement.get("value") if isinstance(measurement, dict) else None
        if not isinstance(value, (int, float)):
            continue

        try:
            start_local = datetime(
                int(row["year"]),
                int(row["month"]),
                int(row["day"]),
                int(row["hour"]),
                int(row["minute"]),
                tzinfo=LOCAL_TIMEZONE,
            )
        except (KeyError, TypeError, ValueError):
            continue

        end_local = start_local + timedelta(minutes=15)
        # Vattenfall provides ore/kWh. Nordpool coordinator expects currency/MWh.
        price_currency_per_mwh = float(value) * 10.0
        converted.append(
            {
                "start": start_local.isoformat(),
                "end": end_local.isoformat(),
                "price": price_currency_per_mwh,
            }
        )

    converted.sort(key=lambda entry: entry["start"])
    return converted


class VattenfallProxyHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves static files and JSON proxy/runtime endpoints."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/vattenfall-prices":
            self._serve_vattenfall_prices(parsed.query)
            return

        if parsed.path == "/":
            self.path = "/date_navigator.html"

        super().do_GET()

    def _serve_vattenfall_prices(self, query: str) -> None:
        params = parse_qs(query)
        raw_date = (params.get("date") or [None])[0]
        raw_hours = (params.get("hours") or [None])[0]

        try:
            selected_day = parse_selected_day(raw_date)
            requested_hours = parse_requested_hours(raw_hours)
        except ValueError as error:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"error": str(error)},
            )
            return

        delivery_start = start_epoch_for_day(selected_day)
        delivery_end = delivery_start + int(
            timedelta(hours=requested_hours).total_seconds()
        )

        try:
            payload = _fetch_vattenfall_payload(delivery_start, delivery_end)
        except UpstreamRequestError as error:
            self._write_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "Upstream request failed",
                    "message": str(error),
                },
            )
            return

        payload["deliveryStart"] = delivery_start
        payload["deliveryEnd"] = delivery_end
        payload["requestedHours"] = requested_hours
        payload["deliveryStartIsoUtc"] = datetime.fromtimestamp(
            delivery_start, tz=UTC
        ).isoformat()
        payload["deliveryEndIsoUtc"] = datetime.fromtimestamp(
            delivery_end, tz=UTC
        ).isoformat()

        self._write_json(HTTPStatus.OK, payload)

    def _write_json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run local web server for Vattenfall Nord Pool histogram page."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to.")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to.")
    parser.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        choices=_LOG_LEVEL_CHOICES,
        help="Process log level (default: INFO).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _configure_logging(args.log_level)
    server = ThreadingHTTPServer((args.host, args.port), VattenfallProxyHandler)
    logging.getLogger(__name__).info("Serving on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
