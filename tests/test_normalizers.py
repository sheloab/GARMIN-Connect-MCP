from mcp_garmin_connect.normalizers import normalize_activity, pace_from_speed, sport_bucket


def test_sport_bucket_maps_common_types() -> None:
    assert sport_bucket("running") == "run"
    assert sport_bucket("cycling") == "bike"
    assert sport_bucket("lap_swimming") == "swim"
    assert sport_bucket("unknown_type") == "other"
    assert sport_bucket(None) is None


def test_pace_from_speed_formats_min_per_km() -> None:
    assert pace_from_speed(4.0) == "4:10"
    assert pace_from_speed(0) is None


def test_normalize_activity_run() -> None:
    activity = {
        "activityId": 123,
        "activityName": "Morning Run",
        "activityType": {"typeKey": "running"},
        "startTimeLocal": "2026-08-11 06:30:00",
        "duration": 1800,
        "distance": 5000,
        "averageHR": 142,
        "maxHR": 171,
        "averageSpeed": 2.777777,
    }

    normalized = normalize_activity(activity)

    assert normalized == {
        "activity_id": 123,
        "name": "Morning Run",
        "sport": "run",
        "type_key": "running",
        "date": "2026-08-11T06:30:00",
        "duration_min": 30.0,
        "duration": "30:00",
        "distance_km": 5.0,
        "hr_avg": 142,
        "hr_max": 171,
        "pace_min_km": "6:00",
    }
