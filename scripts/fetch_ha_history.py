#!/usr/bin/env python3
"""Fetch historical sensor data from a live Home Assistant instance.

Usage:
    python scripts/fetch_ha_history.py [--entity ENTITY_ID] [--days N] [--output FILE]

Credentials are read from custom_components/electricitypricelevels/dev_config.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Add project root to path so we can import dev_config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "custom_components" / "electricitypricelevels"))

try:
    from dev_config import HA_URL, HA_TOKEN
except ImportError:
    print("ERROR: dev_config.py not found or missing HA_URL/HA_TOKEN.")
    print("Create custom_components/electricitypricelevels/dev_config.py with:")
    print('  HA_URL = "http://your-ha-instance:8123"')
    print('  HA_TOKEN = "your-long-lived-access-token"')
    sys.exit(1)

# Default entities to fetch (solar surplus scheduler related, with remote HA prefix)
DEFAULT_ENTITIES = [
    "sensor.balder_batteries_state_of_capacity",
    "sensor.balder_pool_julgran_power",
    "sensor.balder_varmvatten_effekt",
    "sensor.balder_krypgrund_effekt",
    "sensor.balder_nord_pool_se4_current_price",
]


def fetch_history(
    entity_ids: list[str],
    days: int = 7,
    minimal_response: bool = True,
) -> dict[str, list]:
    """Fetch history from HA REST API.

    Returns a dict mapping entity_id -> list of state records.
    Each record: {state, last_changed, attributes (if not minimal)}.
    """
    if not HA_URL or not HA_TOKEN:
        print("ERROR: HA_URL and HA_TOKEN must be set in dev_config.py")
        sys.exit(1)

    start_time = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    entity_filter = ",".join(entity_ids)

    url = f"{HA_URL.rstrip('/')}/api/history/period/{start_time}"
    params = f"filter_entity_id={entity_filter}"
    if minimal_response:
        params += "&minimal_response&no_attributes"
    full_url = f"{url}?{params}"

    req = Request(full_url)
    req.add_header("Authorization", f"Bearer {HA_TOKEN}")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as e:
        print(f"HTTP Error {e.code}: {e.reason}")
        if e.code == 401:
            print("Check your HA_TOKEN in dev_config.py")
        sys.exit(1)
    except URLError as e:
        print(f"Connection error: {e.reason}")
        print(f"Check HA_URL in dev_config.py: {HA_URL}")
        sys.exit(1)

    # HA returns a list of lists, one per entity
    result = {}
    for entity_history in data:
        if not entity_history:
            continue
        entity_id = entity_history[0].get("entity_id", "unknown")
        result[entity_id] = entity_history

    return result


def fetch_current_states(entity_ids: list[str]) -> dict[str, dict]:
    """Fetch current state for each entity."""
    if not HA_URL or not HA_TOKEN:
        print("ERROR: HA_URL and HA_TOKEN must be set in dev_config.py")
        sys.exit(1)

    result = {}
    for entity_id in entity_ids:
        url = f"{HA_URL.rstrip('/')}/api/states/{entity_id}"
        req = Request(url)
        req.add_header("Authorization", f"Bearer {HA_TOKEN}")
        req.add_header("Content-Type", "application/json")

        try:
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                result[entity_id] = data
        except HTTPError as e:
            print(f"  {entity_id}: HTTP {e.code} ({e.reason})")
        except URLError as e:
            print(f"  {entity_id}: Connection error ({e.reason})")

    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch HA sensor history for development")
    parser.add_argument(
        "--entity", "-e",
        action="append",
        help="Entity ID to fetch (can be repeated). Defaults to surplus scheduler entities.",
    )
    parser.add_argument(
        "--days", "-d",
        type=int,
        default=7,
        help="Number of days of history to fetch (default: 7)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Output JSON file path (default: scripts/ha_history.json)",
    )
    parser.add_argument(
        "--current",
        action="store_true",
        help="Fetch current states only (no history)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all available entities matching a pattern",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default=None,
        help="Pattern to filter entity list (used with --list)",
    )

    args = parser.parse_args()
    entities = args.entity or DEFAULT_ENTITIES

    if args.list:
        # Fetch all states and filter
        url = f"{HA_URL.rstrip('/')}/api/states"
        req = Request(url)
        req.add_header("Authorization", f"Bearer {HA_TOKEN}")
        req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=30) as resp:
                all_states = json.loads(resp.read().decode())
        except (HTTPError, URLError) as e:
            print(f"Error: {e}")
            sys.exit(1)

        pattern = (args.pattern or "").lower()
        for state in sorted(all_states, key=lambda s: s["entity_id"]):
            eid = state["entity_id"]
            if pattern and pattern not in eid.lower():
                continue
            friendly = state.get("attributes", {}).get("friendly_name", "")
            val = state.get("state", "?")
            print(f"  {eid:<50} = {val:<15} ({friendly})")
        return

    if args.current:
        print(f"Fetching current states from {HA_URL}...")
        states = fetch_current_states(entities)
        for eid, data in states.items():
            print(f"\n  {eid}:")
            print(f"    state: {data.get('state')}")
            attrs = data.get("attributes", {})
            for k, v in list(attrs.items())[:10]:
                print(f"    {k}: {v}")
        return

    print(f"Fetching {args.days} days of history from {HA_URL}...")
    print(f"  Entities: {', '.join(entities)}")

    history = fetch_history(entities, days=args.days)

    output_path = args.output or str(PROJECT_ROOT / "scripts" / "ha_history.json")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(history, f, indent=2, default=str)

    print(f"\nSaved to {output_path}")
    for eid, records in history.items():
        print(f"  {eid}: {len(records)} records")


if __name__ == "__main__":
    main()
