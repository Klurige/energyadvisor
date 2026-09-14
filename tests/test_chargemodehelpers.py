from custom_components.energyadvisor.sensor.chargemodehelpers import find_peaks_in_modes

MODES = [
    {"from": "2026-08-16T00:00", "mode": "maxuse", "cost": 1.0},
    {"from": "2026-08-16T07:15", "mode": "charge", "cost": 0.7034},
    {"from": "2026-08-16T07:30", "mode": "maxuse", "cost": 0.69991},
    {"from": "2026-08-16T07:45", "mode": "discharge", "cost": 0.70686},
    {"from": "2026-08-16T08:00", "mode": "sell", "cost": 0.70845},
    {"from": "2026-08-16T08:15", "mode": "standby", "cost": 0.70788},
    {"from": "2026-08-16T08:30", "mode": "charge", "cost": 0.70672},
    {"from": "2026-08-16T08:45", "mode": "maxuse", "cost": 0.70266},
    {"from": "2026-08-16T09:00", "mode": "discharge", "cost": 0.70658},
    {"from": "2026-08-16T09:15", "mode": "sell", "cost": 0.69977},
    {"from": "2026-08-16T09:30", "mode": "standby", "cost": 0.7034},
    {"from": "2026-08-16T09:45", "mode": "charge", "cost": 0.70266},
    {"from": "2026-08-16T10:00", "mode": "maxuse", "cost": 0.70266},
    {"from": "2026-08-16T10:15", "mode": "discharge", "cost": 0.69629},
    {"from": "2026-08-16T10:30", "mode": "sell", "cost": 0.69296},
    {"from": "2026-08-16T10:45", "mode": "standby", "cost": 0.69267},
    {"from": "2026-08-16T11:00", "mode": "charge", "cost": 0.69311},
    {"from": "2026-08-16T11:15", "mode": "maxuse", "cost": 0.69136},
    {"from": "2026-08-16T11:30", "mode": "discharge", "cost": 0.68977},
    {"from": "2026-08-16T11:45", "mode": "sell", "cost": 0.68645},
    {"from": "2026-08-16T12:00", "mode": "standby", "cost": 0.68948},
    {"from": "2026-08-16T12:15", "mode": "charge", "cost": 0.69064},
    {"from": "2026-08-16T12:30", "mode": "maxuse", "cost": 0.69064},
    {"from": "2026-08-16T12:45", "mode": "discharge", "cost": 0.69049},
    {"from": "2026-08-16T13:00", "mode": "sell", "cost": 0.69122},
    {"from": "2026-08-16T13:15", "mode": "standby", "cost": 0.6921},
    {"from": "2026-08-16T13:30", "mode": "charge", "cost": 0.69224},
    {"from": "2026-08-16T13:45", "mode": "maxuse", "cost": 0.69296},
    {"from": "2026-08-16T14:00", "mode": "discharge", "cost": 0.68571},
    {"from": "2026-08-16T14:15", "mode": "sell", "cost": 0.6921},
    {"from": "2026-08-16T14:30", "mode": "standby", "cost": 0.69325},
    {"from": "2026-08-16T14:45", "mode": "charge", "cost": 0.70368},
    {"from": "2026-08-16T15:00", "mode": "maxuse", "cost": 0.6889},
    {"from": "2026-08-16T15:15", "mode": "discharge", "cost": 0.69253},
    {"from": "2026-08-16T15:30", "mode": "sell", "cost": 0.7034},
    {"from": "2026-08-16T15:45", "mode": "standby", "cost": 0.71396},
    {"from": "2026-08-16T16:00", "mode": "charge", "cost": 0.69354},
    {"from": "2026-08-16T16:15", "mode": "maxuse", "cost": 0.70542},
    {"from": "2026-08-16T16:30", "mode": "discharge", "cost": 0.74888},
    {"from": "2026-08-16T16:45", "mode": "sell", "cost": 0.85347},
    {"from": "2026-08-16T17:00", "mode": "standby", "cost": 0.73208},
    {"from": "2026-08-16T17:15", "mode": "charge", "cost": 0.81262},
    {"from": "2026-08-16T17:30", "mode": "maxuse", "cost": 0.92951},
    {"from": "2026-08-16T17:45", "mode": "discharge", "cost": 0.99644},
    {"from": "2026-08-16T18:00", "mode": "sell", "cost": 0.83072},
    {"from": "2026-08-16T18:15", "mode": "standby", "cost": 0.89562},
    {"from": "2026-08-16T18:30", "mode": "charge", "cost": 0.99341},
    {"from": "2026-08-16T18:45", "mode": "maxuse", "cost": 1.43363},
    {"from": "2026-08-16T19:00", "mode": "discharge", "cost": 1.50825},
    {"from": "2026-08-16T19:15", "mode": "sell", "cost": 1.19838},
    {"from": "2026-08-16T19:30", "mode": "standby", "cost": 1.6095},
    {"from": "2026-08-16T19:45", "mode": "charge", "cost": 2.038},  # peak
    {"from": "2026-08-16T20:00", "mode": "maxuse", "cost": 1.83404},
    {"from": "2026-08-16T20:15", "mode": "discharge", "cost": 1.90082},
    {"from": "2026-08-16T20:30", "mode": "sell", "cost": 1.70974},
    {"from": "2026-08-16T20:45", "mode": "standby", "cost": 1.76494},
    {"from": "2026-08-16T21:00", "mode": "charge", "cost": 1.64065},
    {"from": "2026-08-16T21:15", "mode": "maxuse", "cost": 1.61067},
    {"from": "2026-08-16T21:30", "mode": "discharge", "cost": 1.40017},
    {"from": "2026-08-16T21:45", "mode": "sell", "cost": 1.00007},
    {"from": "2026-08-16T22:00", "mode": "standby", "cost": 2.07219},  # peak
    {"from": "2026-08-16T22:15", "mode": "charge", "cost": 1.34788},
    {"from": "2026-08-16T22:30", "mode": "maxuse", "cost": 1.39945},
    {"from": "2026-08-16T22:45", "mode": "discharge", "cost": 0.95081},
    {"from": "2026-08-16T23:00", "mode": "sell", "cost": 1.93833},  # peak
    {"from": "2026-08-16T23:15", "mode": "standby", "cost": 1.32195},
    {"from": "2026-08-16T23:30", "mode": "charge", "cost": 1.05352},
    {"from": "2026-08-16T23:45", "mode": "maxuse", "cost": 0.80697},
]


def test_find_peaks_in_modes_returns_list():
    result = find_peaks_in_modes(MODES, 0.7)
    assert isinstance(result, list)
    assert result[0].get("from") == "2026-08-16T19:45"
    assert result[1].get("from") == "2026-08-16T22:00"
    assert result[2].get("from") == "2026-08-16T23:00"
    assert len(result) == 3
