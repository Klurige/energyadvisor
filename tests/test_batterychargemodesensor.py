import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.energyadvisor.const import (
    CONF_BATTERY_DEGRADATION_COST,
    CONF_EXCLUDE_FROM_RECORDING,
)
from custom_components.energyadvisor.sensor.batterychargemodesensor import (
    BatteryChargeModeSensor,
)

TEST_TIMEZONE = ZoneInfo("Europe/Stockholm")


def _slot(
    start: datetime.datetime,
    minutes: int,
    cost: float = 1.0,
    credit: float = 0.5,
    rank: int | str | None = None,
) -> dict:
    slot = {
        "start": start,
        "end": start + datetime.timedelta(minutes=minutes),
        "cost": cost,
        "credit": credit,
    }
    if rank is not None:
        slot["rank"] = rank
    return slot


def _ranked_slots(
    costs: list[float], slot_minutes: int = 60, start_minute: int = 0
) -> list[dict]:
    """Back-to-back slots from 2026-08-15 with minute-scaled ranks."""
    start = datetime.datetime(2026, 8, 15, 0, 0, tzinfo=TEST_TIMEZONE)
    start += datetime.timedelta(minutes=start_minute)
    order = sorted(range(len(costs)), key=lambda i: costs[i])
    ranks = {i: position * 60 for position, i in enumerate(order)}
    return [
        _slot(
            start + datetime.timedelta(minutes=slot_minutes * i),
            slot_minutes,
            cost=cost,
            rank=ranks[i],
        )
        for i, cost in enumerate(costs)
    ]


def _modes_for(
    costs: list[float],
    margin: float | None = None,
    slot_minutes: int = 60,
    start_minute: int = 0,
) -> list[str]:
    sensor = _build_sensor(
        _ranked_slots(costs, slot_minutes, start_minute), margin=margin
    )
    return [m["mode"] for m in sensor._build_modes()]


def _quarter_hour_rates() -> list[dict]:
    first = datetime.datetime(2026, 8, 15, 23, 45, tzinfo=TEST_TIMEZONE)
    return [
        _slot(first + datetime.timedelta(minutes=15 * i), 15, 1.0 + i, 0.25 * i)
        for i in range(4)
    ]


def _build_sensor(
    rates: list[dict], margin: float | None = None
) -> BatteryChargeModeSensor:
    hass = MagicMock()
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.unique_id = "test_unique_id"
    entry.options = {CONF_EXCLUDE_FROM_RECORDING: True}
    if margin is not None:
        entry.options[CONF_BATTERY_DEGRADATION_COST] = margin

    price_sensor = MagicMock()
    price_sensor._rates = rates
    price_sensor.async_add_update_listener.return_value = MagicMock()

    sensor = BatteryChargeModeSensor(
        hass, entry, MagicMock(spec=DeviceInfo), price_sensor
    )
    sensor.async_write_ha_state = MagicMock()
    return sensor


def test_modes_are_standby_without_ranks_and_have_matching_times() -> None:
    sensor = _build_sensor(_quarter_hour_rates())

    modes = sensor._build_modes()

    assert [(m["from"], m["to"]) for m in modes] == [
        ("2026-08-15T23:45", "2026-08-16T00:00"),
        ("2026-08-16T00:00", "2026-08-16T00:15"),
        ("2026-08-16T00:15", "2026-08-16T00:30"),
        ("2026-08-16T00:30", "2026-08-16T00:45"),
    ]
    assert {m["mode"] for m in modes} == {"standby"}


def test_modes_carry_cost_and_credit_from_the_price_slots_and_target_soc_50() -> None:
    sensor = _build_sensor(_quarter_hour_rates())

    modes = sensor._build_modes()

    assert [m["cost"] for m in modes] == [1.0, 2.0, 3.0, 4.0]
    assert [m["credit"] for m in modes] == [0.0, 0.25, 0.5, 0.75]
    assert {m["target_soc"] for m in modes} == {50}


def test_modes_round_cost_and_credit_to_two_decimals() -> None:
    start = datetime.datetime(2026, 8, 15, 0, 0, tzinfo=TEST_TIMEZONE)
    sensor = _build_sensor([_slot(start, 15, cost=1.23456, credit=0.98765)])

    assert sensor._build_modes() == [
        {
            "from": "2026-08-15T00:00",
            "to": "2026-08-15T00:15",
            "mode": "standby",
            "target_soc": 50,
            "cost": 1.23,
            "credit": 0.99,
        }
    ]


def test_modes_follow_slot_length_of_the_price_slots() -> None:
    start = datetime.datetime(2026, 8, 15, 0, 0, tzinfo=TEST_TIMEZONE)
    sensor = _build_sensor(
        [_slot(start, 60), _slot(start + datetime.timedelta(hours=1), 60)]
    )

    assert [(m["from"], m["to"]) for m in sensor._build_modes()] == [
        ("2026-08-15T00:00", "2026-08-15T01:00"),
        ("2026-08-15T01:00", "2026-08-15T02:00"),
    ]


def test_modes_are_empty_without_price_data() -> None:
    sensor = _build_sensor([])

    assert sensor._build_modes() == []
    assert sensor.state == "standby"
    assert sensor.extra_state_attributes == {"modes": []}


def test_slots_without_times_are_skipped() -> None:
    start = datetime.datetime(2026, 8, 15, 0, 0, tzinfo=TEST_TIMEZONE)
    sensor = _build_sensor([{"cost": 1.0}, _slot(start, 15)])

    assert len(sensor._build_modes()) == 1


async def test_added_to_hass_builds_modes_and_listens_to_price_updates() -> None:
    sensor = _build_sensor(_quarter_hour_rates())
    sensor.async_on_remove = MagicMock()

    await sensor.async_added_to_hass()

    assert len(sensor.extra_state_attributes["modes"]) == 4
    sensor._price_sensor.async_add_update_listener.assert_called_once_with(
        sensor._handle_price_update
    )
    sensor.async_on_remove.assert_called_once_with(
        sensor._price_sensor.async_add_update_listener.return_value
    )


def test_price_update_rebuilds_modes_and_writes_state() -> None:
    sensor = _build_sensor(_quarter_hour_rates())
    assert sensor.extra_state_attributes["modes"] == []

    sensor._handle_price_update()
    assert len(sensor.extra_state_attributes["modes"]) == 4
    sensor.async_write_ha_state.assert_called_once()

    sensor._price_sensor._rates = _quarter_hour_rates()[:2]
    sensor._handle_price_update()
    assert len(sensor.extra_state_attributes["modes"]) == 2
    assert sensor.state == "standby"


def test_modes_attribute_is_unrecorded() -> None:
    sensor = _build_sensor([])

    assert "modes" in sensor._unrecorded_attributes


def test_peak_after_a_valley_discharges_and_the_valley_charges() -> None:
    assert _modes_for([1.0, 5.0], margin=2.0) == ["charge", "discharge"]


def test_expensive_slot_without_an_earlier_cheaper_slot_is_not_a_peak() -> None:
    assert _modes_for([5.0, 1.0], margin=2.0) == ["standby", "standby"]


def test_spread_below_the_cost_margin_gives_no_peak() -> None:
    assert _modes_for([1.0, 1.5, 1.2], margin=1.0) == ["standby"] * 3


def test_spread_equal_to_the_cost_margin_is_a_peak() -> None:
    assert _modes_for([1.0, 3.0], margin=2.0) == ["charge", "discharge"]


def test_zero_or_missing_margin_still_requires_a_strictly_cheaper_valley() -> None:
    assert _modes_for([1.0, 1.0, 2.0], margin=0.0) == ["standby", "charge", "discharge"]
    assert _modes_for([2.0, 2.0]) == ["standby", "standby"]
    assert _modes_for([1.0, 2.0]) == ["charge", "discharge"]


def test_peak_is_the_single_highest_slot_after_the_valley() -> None:
    # 6.0 is also high, but it sits right next to the highest slot (7.0): only
    # one slot of a hump becomes the peak.
    assert _modes_for([1.0, 5.0, 6.0, 7.0, 2.0], margin=2.0) == [
        "charge",
        "standby",
        "standby",
        "discharge",
        "standby",
    ]


def test_first_valley_is_the_lowest_slot_before_the_first_peak() -> None:
    assert _modes_for([3.0, 1.0, 2.0, 9.0], margin=2.0) == [
        "standby",
        "charge",
        "standby",
        "discharge",
    ]


def test_valley_between_two_peaks_is_the_single_lowest_slot_between_them() -> None:
    # 4-hour slots: peaks 8.0 (04:00) and 9.0 (20:00); the lowest slot between
    # them is 3.0, not 5.0 or 4.0. The first valley is the lowest slot since
    # midnight (1.0).
    assert _modes_for([1.0, 8.0, 5.0, 3.0, 4.0, 9.0], margin=2.0, slot_minutes=240) == [
        "charge",
        "discharge",
        "standby",
        "charge",
        "standby",
        "discharge",
    ]


def test_peaks_and_valleys_alternate_over_several_humps() -> None:
    # 6-hour slots over two days: one peak before and one after noon each day.
    costs = [2.0, 6.0, 3.0, 9.0, 1.0, 8.0, 0.5, 7.0]

    assert _modes_for(costs, margin=2.0, slot_minutes=360) == [
        "charge",
        "discharge",
        "charge",
        "discharge",
        "charge",
        "discharge",
        "charge",
        "discharge",
    ]


def test_second_peak_needs_a_valley_at_least_the_margin_below_it() -> None:
    costs = [1.0, 9.0, 8.5, 9.1]

    assert _modes_for(costs, margin=0.7, slot_minutes=240) == [
        "charge",
        "standby",
        "standby",
        "discharge",
    ]
    assert _modes_for(costs, margin=0.5, slot_minutes=240) == [
        "charge",
        "discharge",
        "charge",
        "discharge",
    ]


def test_at_most_one_peak_before_noon_and_one_after_noon() -> None:
    # 3-hour slots: 00:00-09:00 are before noon, 12:00-21:00 after noon. The
    # 7.0 (09:00) and 6.0 (21:00) slots would be peaks without the limit.
    costs = [1.0, 8.0, 2.0, 7.0, 1.5, 9.0, 3.0, 6.0]

    assert _modes_for(costs, margin=2.0, slot_minutes=180) == [
        "charge",
        "discharge",
        "standby",
        "standby",
        "charge",
        "discharge",
        "standby",
        "standby",
    ]


def test_half_day_limit_is_applied_per_day() -> None:
    # 12-hour slots over two days: 00:00 and 12:00 of each day.
    costs = [1.0, 9.0, 2.0, 8.0]

    assert _modes_for(costs, margin=2.0, slot_minutes=720) == [
        "charge",
        "discharge",
        "charge",
        "discharge",
    ]


def test_invalid_highest_slot_of_a_half_day_does_not_block_the_next_one() -> None:
    # 3-hour slots before noon. 9.0 is the first slot and has no valley before
    # it, so the second highest slot (5.0) becomes the peak.
    assert _modes_for([9.0, 1.0, 5.0, 2.0], margin=2.0, slot_minutes=180) == [
        "standby",
        "charge",
        "discharge",
        "standby",
    ]


def test_noon_slot_belongs_to_the_afternoon() -> None:
    # 30-minute slots from 10:30: 11:00 is a peak before noon, 12:00 a peak
    # after noon, and 11:30 is the valley between them.
    assert _modes_for(
        [1.0, 8.0, 2.0, 9.0], margin=2.0, slot_minutes=30, start_minute=630
    ) == ["charge", "discharge", "charge", "discharge"]


def test_adjacent_slots_are_never_two_peaks() -> None:
    assert _modes_for([1.0, 2.0, 9.0, 8.0], margin=3.0) == [
        "charge",
        "standby",
        "discharge",
        "standby",
    ]


def test_peak_without_a_valley_between_it_and_the_previous_peak_is_dropped() -> None:
    assert _modes_for([1.0, 9.0, 8.0, 7.0], margin=3.0) == [
        "charge",
        "discharge",
        "standby",
        "standby",
    ]


def test_valley_must_come_before_the_peak() -> None:
    assert _modes_for([2.0, 9.0, 5.0, 1.0], margin=3.0) == [
        "charge",
        "discharge",
        "standby",
        "standby",
    ]


def test_latest_slot_wins_when_valleys_cost_the_same() -> None:
    assert _modes_for([1.0, 1.0, 5.0], margin=2.0) == [
        "standby",
        "charge",
        "discharge",
    ]


def test_first_valley_is_searched_since_midnight_of_the_peaks_day() -> None:
    start = datetime.datetime(2026, 8, 15, 22, 0, tzinfo=TEST_TIMEZONE)
    costs = [0.5, 1.0, 2.0, 9.0]  # 22:00, 23:00 | 00:00, 01:00 next day
    ranks = [0, 60, 120, 180]
    sensor = _build_sensor(
        [
            _slot(start + datetime.timedelta(hours=i), 60, cost=c, rank=r)
            for i, (c, r) in enumerate(zip(costs, ranks))
        ],
        margin=2.0,
    )

    assert [m["mode"] for m in sensor._build_modes()] == [
        "standby",
        "standby",
        "charge",
        "discharge",
    ]


def test_slots_with_unknown_rank_are_never_peaks_but_can_be_valleys() -> None:
    start = datetime.datetime(2026, 8, 15, 0, 0, tzinfo=TEST_TIMEZONE)
    slots = [
        _slot(start, 60, cost=1.0, rank="N/A"),
        _slot(start + datetime.timedelta(hours=1), 60, cost=9.0, rank="N/A"),
        _slot(start + datetime.timedelta(hours=2), 60, cost=8.0, rank=60),
    ]
    sensor = _build_sensor(slots, margin=2.0)

    assert [m["mode"] for m in sensor._build_modes()] == [
        "charge",
        "standby",
        "discharge",
    ]


def test_invalid_cost_margin_is_treated_as_zero() -> None:
    sensor = _build_sensor(_ranked_slots([1.0, 2.0]))
    sensor._entry.options[CONF_BATTERY_DEGRADATION_COST] = "abc"

    assert [m["mode"] for m in sensor._build_modes()] == ["charge", "discharge"]


def test_planned_modes_keep_times_cost_and_target_soc() -> None:
    sensor = _build_sensor(_ranked_slots([1.0, 5.0]), margin=2.0)

    assert sensor._build_modes() == [
        {
            "from": "2026-08-15T00:00",
            "to": "2026-08-15T01:00",
            "mode": "charge",
            "target_soc": 50,
            "cost": 1.0,
            "credit": 0.5,
        },
        {
            "from": "2026-08-15T01:00",
            "to": "2026-08-15T02:00",
            "mode": "discharge",
            "target_soc": 50,
            "cost": 5.0,
            "credit": 0.5,
        },
    ]
