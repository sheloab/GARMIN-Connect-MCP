from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from mcp_garmin_connect.config import MAX_DAYS_HEALTH, Settings
from mcp_garmin_connect.errors import GarminAuthError
from mcp_garmin_connect.garmin_client import GarminClientManager, reset_manager
from mcp_garmin_connect.tools import (
    TOOL_SPECS,
    _hr_drift,
    _stress_band,
    get_activity_distance_splits,
    get_activity_segments,
    get_activity_splits,
    get_body_battery,
    get_body_battery_range,
    get_daily_activity,
    get_hrv_history,
    get_monthly_running_stats,
    get_performance_metrics,
    get_personal_records,
    get_recent_activities,
    get_recent_load,
    get_resting_heart_rate,
    get_running_summary,
    get_running_tolerance,
    get_sleep,
    get_sleep_detail,
    get_stress,
    get_training_readiness_history,
    get_workout_detail,
    get_yearly_totals,
    get_zones,
    tool_schema,
)


class FakeGarmin:
    def get_activities(self, start: int, limit: int) -> list[dict[str, Any]]:
        return [
            {
                "activityId": 1,
                "activityName": "Run",
                "activityType": {"typeKey": "running"},
                "startTimeLocal": "2999-01-01 07:00:00",
                "duration": 1800,
                "distance": 5000,
                "averageHR": 140,
            },
            {
                "activityId": 2,
                "activityName": "Ride",
                "activityType": {"typeKey": "cycling"},
                "startTimeLocal": "2999-01-01 08:00:00",
                "duration": 3600,
                "distance": 30000,
                "averageHR": 130,
            },
        ]

    def get_personal_record(self) -> dict[str, Any]:
        return {
            "personalRecords": [
                {
                    "typeId": 3,
                    "prTypeLabelKey": None,
                    "value": "00:30:00",
                    "prStartTimeGmtFormatted": "2999-01-01",
                },
                {
                    "typeId": 4,
                    "value": "01:05:00",
                    "prStartTimeGmtFormatted": "2999-01-02",
                }
            ]
        }

    def get_rhr_day(self, cdate: str) -> dict[str, Any]:
        return {
            "allMetrics": {
                "metricsMap": {
                    "WELLNESS_RESTING_HEART_RATE": [
                        {
                            "calendarDate": cdate,
                            "value": 50,
                        }
                    ]
                }
            }
        }

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        dto: dict[str, Any] = {
            "calendarDate": cdate,
            "sleepTimeSeconds": 24480,
            "napTimeSeconds": 1500,
            "sleepStartTimestampLocal": 1893537000000,
            "sleepEndTimestampLocal": 1893563280000,
            "deepSleepSeconds": 6000,
            "lightSleepSeconds": 13680,
            "remSleepSeconds": 4800,
            "awakeSleepSeconds": 600,
            "averageSpO2Value": 96.0,
            "lowestSpO2Value": 90,
            "averageRespirationValue": 14.0,
            "awakeCount": 1,
            "avgSleepStress": 15.0,
            "avgHeartRate": 52.0,
            "sleepScoreFeedback": "POSITIVE_DEEP",
            "sleepScores": {
                "overall": {"value": 80, "qualifierKey": "GOOD"},
                "remPercentage": {"value": 20, "qualifierKey": "GOOD"},
                "awakeCount": {"qualifierKey": "EXCELLENT"},
            },
            "sleepNeed": {
                "actual": 450,
                "baseline": 450,
                "recommendedBedtimeStartMins": 1350,
                "recommendedBedtimeEndMins": 1380,
            },
            "nextSleepNeed": {"calendarDate": "next", "actual": 480, "napAdjustment": "DECREASING"},
            "dailyNapDTOS": [
                {
                    "napTimeSec": 1500,
                    "napStartTimestampGMT": "2030-01-02T12:10:00",
                    "napEndTimestampGMT": "2030-01-02T12:35:00",
                    "napStartTimeOffset": 60,
                    "napEndTimeOffset": 60,
                    "napFeedback": "IDEAL_TIMING_IDEAL_DURATION_LOW_NEED",
                }
            ],
        }
        dto["sleepStartTimestampGMT"] = 1893533400000
        return {
            "sleepLevels": [
                {"startGMT": "2030-01-01T21:30:00.0", "endGMT": "2030-01-01T21:50:00.0",
                 "activityLevel": 0.0},
                {"startGMT": "2030-01-01T21:50:00.0", "endGMT": "2030-01-01T22:10:00.0",
                 "activityLevel": 0.0},
                {"startGMT": "2030-01-01T22:10:00.0", "endGMT": "2030-01-01T22:20:00.0",
                 "activityLevel": 3.0},
            ],
            "wellnessEpochSPO2DataDTOList": [
                {"epochTimestamp": "2030-01-01T21:40:00.0", "spo2Reading": 88},
                {"epochTimestamp": "2030-01-01T21:41:00.0", "spo2Reading": 95},
            ],
            "dailySleepDTO": dto,
            "avgOvernightHrv": 60.0,
            "hrvStatus": "BALANCED",
            "restingHeartRate": 50,
            "restlessMomentsCount": 15,
            "bodyBatteryChange": 70,
            "avgSkinTempDeviationC": 0.1,
        }

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        return {
            "totalSteps": 12000,
            "dailyStepGoal": 10000,
            "totalDistanceMeters": 9500,
            "floorsAscended": 3.4,
            "moderateIntensityMinutes": 10,
            "vigorousIntensityMinutes": 20,
            "sedentarySeconds": 36000,
            "bodyBatteryAtWakeTime": 90,
            "bodyBatteryDuringSleep": 70,
            "bodyBatteryActivityEventList": [
                {
                    "eventType": "SLEEP",
                    "eventStartTimeGmt": "2030-01-01T21:30:00",
                    "timezoneOffset": 3600000,
                    "durationInMilliseconds": 27000000,
                    "bodyBatteryImpact": 70,
                }
            ],
        }

    def get_weekly_intensity_minutes(self, start: str, end: str) -> list[dict[str, Any]]:
        return [{"calendarDate": "2030-01-01", "weeklyGoal": 150, "moderateValue": 40,
                 "vigorousValue": 30}]

    def get_training_readiness(self, cdate: str) -> list[dict[str, Any]]:
        return [
            {"timestamp": "2030-01-01T05:00:00.0", "score": 55, "level": "MODERATE"},
            {
                "timestamp": "2030-01-01T09:00:00.0",
                "score": 70,
                "level": "HIGH",
                "recoveryTime": 90,
                "hrvFactorPercent": 100,
                "hrvFactorFeedback": "VERY_GOOD",
                "hrvWeeklyAverage": 58,
            },
        ]

    def get_endurance_score(self, start: str, end: str) -> dict[str, Any]:
        return {
            "enduranceScoreDTO": {
                "overallScore": 5400,
                "classificationLowerLimitIntermediate": 5100,
                "classificationLowerLimitTrained": 5800,
                "classificationLowerLimitWellTrained": 6500,
            },
            "groupMap": {"2030-01-01": {"groupAverage": 5380, "groupMax": 5400}},
        }

    def get_hill_score(self, start: str, end: str) -> dict[str, Any]:
        return {"hillScoreDTOList": [{"calendarDate": "2030-01-01", "overallScore": 40}]}

    def get_fitnessage_data(self, cdate: str) -> dict[str, Any]:
        return {"chronologicalAge": 40, "fitnessAge": 36.5, "components": {}}

    def get_lactate_threshold(self) -> dict[str, Any]:
        return {
            "speed_and_heart_rate": {"speed": 0.3, "heartRate": 165},
            "power": {"functionalThresholdPower": 350},
        }

    def get_max_metrics_range(self, start: str, end: str) -> list[dict[str, Any]]:
        return [
            {
                "generic": {"calendarDate": "2030-01-01", "vo2MaxPreciseValue": 50.1},
                "heatAltitudeAcclimation": {"heatAcclimationPercentage": 100,
                                            "heatTrend": "ACCLIMATIZED"},
            }
        ]

    def get_running_tolerance(self, start: str, end: str) -> list[dict[str, Any]]:
        return [{"startOfWeek": "2030-01-01", "totalImpactLoad": 5000, "tolerance": 20000,
                 "totalDistance": 12000.0}]

    def get_activity_details(
        self, activity_id: int, maxchart: int = 2000, maxpoly: int = 4000
    ) -> dict[str, Any]:
        keys = [
            "sumDistance",
            "sumDuration",
            "sumElapsedDuration",
            "directSpeed",
            "directHeartRate",
            "directDoubleCadence",
            "directLatitude",
        ]
        rows = []
        for second in range(1101):
            distance = second * 2.5
            rows.append(
                {
                    "metrics": [
                        distance,
                        float(second),
                        float(second) + (60.0 if second == 1100 else 0.0),
                        2.5,
                        130 if distance <= 1375 else 132,
                        170.0,
                        1.23,
                    ]
                }
            )
        return {
            "metricDescriptors": [{"key": k, "metricsIndex": i} for i, k in enumerate(keys)],
            "activityDetailMetrics": rows,
        }

    def get_heart_rate_zones(self) -> list[dict[str, Any]]:
        return [{"sport": "RUNNING", "trainingMethod": "HR_MAX", "maxHeartRateUsed": 190,
                 "restingHeartRateUsed": 50, "zone1Floor": 95, "zone2Floor": 114,
                 "zone3Floor": 133, "zone4Floor": 152, "zone5Floor": 171}]

    def get_power_zones_for_sport(self, sport: str) -> dict[str, Any]:
        return {"sport": sport, "functionalThresholdPower": 300.0, "zone1Floor": 200.0,
                "zone2Floor": 250.0, "zone3Floor": 0.0}

    def get_activity_typed_splits(self, activity_id: int) -> dict[str, Any]:
        return {"splits": [
            {"type": "INTERVAL_ACTIVE", "duration": 1800.0, "elapsedDuration": 1920.0,
             "distance": 5000.0},
            {"type": "RWD_RUN", "startTimeLocal": "2030-01-01T07:00:00.0", "duration": 600.0,
             "distance": 1600.0, "averageHR": 140.0},
            {"type": "RWD_WALK", "startTimeLocal": "2030-01-01T07:10:00.0", "duration": 60.0,
             "distance": 100.0},
            {"type": "RWD_STAND", "startTimeLocal": "2030-01-01T07:11:00.0", "duration": 0.0},
        ]}

    def get_workout_by_id(self, workout_id: int) -> dict[str, Any]:
        return {
            "workoutName": "Intervals",
            "sportType": {"sportTypeKey": "running"},
            "author": {"fullName": "Private Person"},
            "workoutSegments": [{"workoutSteps": [
                {"type": "ExecutableStepDTO", "stepType": {"stepTypeKey": "warmup"},
                 "endCondition": {"conditionTypeKey": "time"}, "endConditionValue": 600.0,
                 "targetType": {"workoutTargetTypeKey": "no.target"}},
                {"type": "RepeatGroupDTO", "numberOfIterations": 5, "workoutSteps": [
                    {"type": "ExecutableStepDTO", "stepType": {"stepTypeKey": "interval"},
                     "endCondition": {"conditionTypeKey": "distance"},
                     "endConditionValue": 1000.0,
                     "targetType": {"workoutTargetTypeKey": "pace.zone"},
                     "targetValueOne": 3.333333, "targetValueTwo": 3.030303},
                ]},
            ]}],
        }

    def get_progress_summary_between_dates(
        self, start: str, end: str, metric: str, group: bool
    ) -> list[dict[str, Any]]:
        if metric == "distance":
            stats = {"running": {"distance": {"count": 10, "sum": 5000000.0, "avg": 500000.0,
                                              "max": 1000000.0}}}
        else:
            stats = {"running": {"duration": {"count": 10, "sum": 18000000.0}}}
        return [{"stats": stats}]

    def get_activity_splits(self, activity_id: int) -> dict[str, Any]:
        if activity_id == 2:
            return {
                "activityId": 2,
                "lapDTOs": [
                    {
                        "distance": 1000.0,
                        "duration": 500.0,
                        "averageSpeed": 2.0,
                        "averageHR": 140,
                        "maxHR": 148,
                        "averageRunCadence": 158,
                        "elevationGain": 4,
                    },
                    {
                        "distance": 1000.0,
                        "duration": 520.0,
                        "averageSpeed": 1.9230769,
                        "averageHR": 146,
                        "maxHR": 152,
                        "averageRunCadence": 156,
                    },
                ],
            }
        if activity_id == 3:
            return {}
        return {
            "activityId": 1,
            "lapDTOs": [
                {
                    "distance": 1000.0,
                    "duration": 480.0,
                    "averageSpeed": 2.0833333,
                    "averageHR": 138,
                    "maxHR": 145,
                    "averageRunCadence": 162,
                    "elevationGain": 3,
                },
                {
                    "distance": 1000.0,
                    "duration": 486.0,
                    "averageSpeed": 2.0576131,
                    "averageHR": 141,
                    "maxHR": 149,
                    "averageRunCadence": 160,
                    "elevationGain": 5,
                },
                {
                    "distance": 1000.0,
                    "duration": 492.0,
                    "averageSpeed": 2.0325203,
                    "averageHR": 145,
                    "maxHR": 153,
                    "averageRunCadence": 159,
                    "elevationGain": 2,
                },
                {
                    "distance": 1000.0,
                    "duration": 498.0,
                    "averageSpeed": 2.0080321,
                    "averageHR": 150,
                    "maxHR": 158,
                    "averageRunCadence": 157,
                    "elevationGain": 6,
                },
                {
                    "distance": 1000.0,
                    "duration": 504.0,
                    "averageSpeed": 1.9841270,
                    "averageHR": 156,
                    "maxHR": 163,
                    "averageRunCadence": 155,
                    "elevationGain": 1,
                },
                {
                    "distance": 1000.0,
                    "duration": 512.0,
                    "averageSpeed": 1.9531250,
                    "averageHR": 161,
                    "maxHR": 168,
                    "averageRunCadence": 153,
                    "elevationGain": 4,
                },
            ],
        }

    display_name = "fake-user"

    def get_user_profile(self) -> dict[str, Any]:
        return {
            "userData": {
                "gender": "MALE",
                "birthDate": "2005-07-11",
                "weight": 73000,
                "heartRateZones": [{"zone": 1, "low": 100, "high": 120}],
                "lactateThresholdHeartRate": 165,
            }
        }

    def connectapi(self, url: str) -> list[dict[str, Any]]:
        assert url == "/personalrecord-service/personalrecordtype/prtypes/fake-user"
        return [
            {
                "id": 3,
                "key": "pr.label.5k.run",
                "visible": True,
                "sport": "RUNNING",
            }
        ]


class FakeManager(GarminClientManager):
    def __init__(self) -> None:
        super().__init__(
            Settings(
                garmin_email="x",
                garmin_password="y",
                garmin_token_store=".",
                deepseek_api_key=None,
                deepseek_base_url="https://api.deepseek.com",
                deepseek_model="deepseek-v4-pro",
                openai_api_key=None,
                openai_base_url="https://api.openai.com/v1",
                openai_model="gpt-5",
                openrouter_api_key=None,
                openrouter_base_url="https://openrouter.ai/api/v1",
                openrouter_model="google/gemini-3-flash-preview",
                gemini_api_key=None,
                gemini_base_url="https://generativelanguage.googleapis.com/v1beta",
                gemini_model="gemini-3.6-flash",
                cache_ttl_seconds=60,
            )
        )
        self._client = FakeGarmin()


def test_recent_activities_with_fake_manager() -> None:
    reset_manager()
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_recent_activities(days=30)

    assert result["count"] == 2
    assert result["activities"][0]["sport"] == "bike"
    assert result["activities"][1]["sport"] == "run"


def test_recent_load_aggregates_by_sport() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_recent_load(days=30)

    assert result["by_sport"]["run"]["total_km"] == 5.0
    assert result["by_sport"]["bike"]["total_hours"] == 1.0


def test_activity_splits_normalizes_laps_and_hr_drift() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_activity_splits(activity_id=1)

    assert result["lap_count"] == 6
    assert "errors" not in result
    first = result["laps"][0]
    assert first["index"] == 1
    assert first["distance_km"] == 1.0
    assert first["duration"] == "8:00"
    assert first["pace"] == "8:00/km"
    assert first["pace_seconds_per_km"] == 480.0
    assert first["hr_avg"] == 138
    assert first["hr_max"] == 145
    assert first["cadence"] == 162
    assert first["elevation_gain_m"] == 3.0
    assert result["laps"][1]["elevation_gain_m"] == 5.0
    assert result["laps"][1]["pace"] == "8:06/km"

    assert result["summary"]["distance_km"] == 6.0
    assert result["summary"]["duration"] == "49:32"
    assert result["summary"]["avg_pace"] == "8:15/km"
    assert result["summary"]["avg_hr"] == 149.0
    assert result["summary"]["avg_cadence"] == 158.0
    assert result["summary"]["elevation_gain_m"] == 21.0

    drift = result["hr_drift"]
    assert drift["first_half_avg_hr"] == 141.0
    assert drift["second_half_avg_hr"] == 156.0
    assert drift["drift_pct"] == 10.1
    assert drift["interpretation"].startswith("drift tinggi")
    assert "hr_drift_note" not in result


def test_activity_splits_reports_when_hr_drift_is_unavailable() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_activity_splits(activity_id=2)

    assert result["lap_count"] == 2
    assert result["laps"][0]["pace"] == "8:20/km"
    assert result["laps"][1]["elevation_gain_m"] is None
    assert result["summary"]["distance_km"] == 2.0
    assert result["summary"]["duration"] == "17:00"
    assert result["summary"]["elevation_gain_m"] == 4.0
    assert result["hr_drift"] is None
    assert result["hr_drift_note"] == "needs at least 4 laps, got 2"


def test_activity_splits_without_data_returns_error() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_activity_splits(activity_id=3)

    assert result["activity_id"] == 3
    assert result["error"] == "splits not found or inaccessible"


@pytest.mark.parametrize(
    ("hr_values", "expected"),
    [
        ([140, 141, 140, 141], "stabil"),
        ([140, 141, 144, 146], "drift ringan"),
        ([140, 145, 155, 160], "drift tinggi (cari penyebab: panas/dehidrasi/kelelahan)"),
    ],
)
def test_hr_drift_interpretation_thresholds(hr_values: list[int], expected: str) -> None:
    laps = [{"hr_avg": value} for value in hr_values]

    drift, note = _hr_drift(laps)

    assert note is None
    assert drift is not None
    assert drift["interpretation"] == expected


def test_hr_drift_needs_all_laps() -> None:
    drift, note = _hr_drift([{"hr_avg": 140}, {"hr_avg": 145}, {"hr_avg": 150}, {"hr_avg": None}])

    assert drift is None
    assert note == "not every lap reports an average heart rate"


def test_running_summary() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_running_summary(days=90)

    assert result["sessions"] == 1
    assert result["total_distance_km"] == 5.0
    assert result["average_pace"] == "6:00/km"
    assert result["longest_run"]["name"] == "Run"


def test_monthly_running_stats() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_monthly_running_stats(months=3)

    assert result["months"][0]["month"] == "2999-01"
    assert result["months"][0]["sessions"] == 1


def test_personal_records_uses_garminconnect_singular_method() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_personal_records()

    assert result["count"] == 2
    assert result["records"][0]["name"] == "Fastest 5K"
    assert result["records"][0]["label_key"] == "pr.label.5k.run"
    assert result["records"][0]["sport"] == "RUNNING"
    assert result["records"][1]["name"] == "Fastest 10K"


def test_resting_heart_rate_daily_weekly_monthly_summary() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_resting_heart_rate(days=3)

    assert result["period_days"] == 3
    assert result["latest_bpm"] == 50
    assert result["weekly_avg_bpm"] == 50
    assert result["monthly_avg_bpm"] == 50
    assert len(result["daily"]) == 3


def test_resting_heart_rate_clamps_to_the_health_cap() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_resting_heart_rate(days=365)

    # The cap is configurable (GARMIN_MAX_DAYS_HEALTH), so assert against it
    # rather than a hardcoded 30.
    assert result["period_days"] == MAX_DAYS_HEALTH
    assert len(result["daily"]) == MAX_DAYS_HEALTH


def test_sleep_includes_naps_stages_and_vitals() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_sleep(days=2)
    day = result["daily"][0]

    assert day["hours"] == 6.8
    assert day["score"] == 80
    assert day["score_rating"] == "GOOD"
    assert day["score_factors"] == {"awake_count": "EXCELLENT"}
    assert day["bedtime"] == "2030-01-01T22:30"
    assert day["wake_time"] == "2030-01-02T05:48"
    assert day["stages"]["deep"] == {"minutes": 100}
    assert day["stages"]["rem"] == {"minutes": 80, "percent": 20, "rating": "GOOD"}
    assert day["spo2"]["lowest"] == 90
    assert day["overnight_hrv_ms"] == 60.0
    assert day["sleep_need"]["hours"] == 7.5
    assert day["sleep_need"]["recommended_bedtime"] == "22:30-23:00"
    assert day["nap_minutes"] == 25
    assert day["naps"] == [
        {
            "start": "2030-01-02T13:10",
            "end": "2030-01-02T13:35",
            "minutes": 25,
            "feedback": "IDEAL_TIMING_IDEAL_DURATION_LOW_NEED",
        }
    ]
    assert result["nap_count"] == 2
    assert result["total_nap_minutes"] == 50
    assert result["avg_rem_minutes"] == 80
    assert result["next_sleep_need"]["hours"] == 8.0
    assert result["next_sleep_need"]["adjustments"]["nap"] == "DECREASING"


def test_daily_activity_and_weekly_intensity() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_daily_activity(days=2)

    assert result["total_steps"] == 24000
    assert result["step_goal_days_met"] == 2
    day = result["daily"][0]
    assert day["distance_km"] == 9.5
    assert day["floors_climbed"] == 3.4
    assert day["activity_minutes"]["sedentary"] == 600
    assert result["weekly_intensity_minutes"][0]["total"] == 100


def test_body_battery_events_use_local_time() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    day = get_body_battery(days=1)["daily"][0]

    assert day["at_wake"] == 90
    assert day["events"][0] == {
        "type": "SLEEP",
        "start": "2030-01-01T22:30",
        "minutes": 450,
        "impact": 70,
        "feedback": None,
        "activity_name": None,
        "activity_id": None,
    }


def test_training_readiness_history_uses_latest_update() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    day = get_training_readiness_history(days=1)["daily"][0]

    assert day["score"] == 70
    assert day["factors"]["recovery_time"]["hours"] == 1.5
    assert day["factors"]["hrv"] == {"percent": 100, "feedback": "VERY_GOOD", "weekly_avg_ms": 58}


def test_performance_metrics() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_performance_metrics()

    assert result["endurance_score"]["level"] == "INTERMEDIATE"
    assert result["endurance_score"]["next_level"] == {"level": "TRAINED", "points_needed": 400}
    assert result["hill_score"]["score"] == 40
    assert result["lactate_threshold"]["pace_per_km"] == "5:33/km"
    assert result["lactate_threshold"]["running_ftp_w"] == 350
    assert result["acclimation"]["heat_percent"] == 100
    assert result["vo2_max_trend"] == [{"date": "2030-01-01", "vo2_max": 50.1}]


def test_running_tolerance_percent() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    week = get_running_tolerance(weeks=1)["weekly"][0]

    assert week["percent_of_tolerance"] == 25
    assert week["distance_km"] == 12.0


def test_activity_distance_splits_from_stream() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_activity_distance_splits(activity_id=1, split_km=1.0)

    assert result["split_count"] == 3
    first = result["splits"][0]
    assert first["distance_m"] == 1000
    assert first["pace"] == "6:40/km"
    assert first["hr_avg"] == 130
    assert first["cadence_spm"] == 170
    assert "latitude" not in str(first).lower()
    assert result["splits"][2]["partial"] is True
    assert result["splits"][2]["distance_m"] == 750
    assert result["split_pattern"] == "even"
    assert result["stopped_time"] == "1:00"
    assert result["aerobic_decoupling"]["percent"] == 1.5


def test_zones_include_hr_and_power_ranges() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_zones()

    zones = result["heart_rate_zones"][0]["zones_bpm"]
    assert zones[1] == {"zone": 2, "low": 114, "high": 132}
    assert zones[-1] == {"zone": 5, "low": 171, "high": 190}
    assert result["running_power_zones"]["zones_w"] == [
        {"zone": 1, "low": 200, "high": 249},
        {"zone": 2, "low": 250, "high": None},
    ]


def test_sleep_detail_merges_stages_in_local_time() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_sleep_detail(date="2030-01-02")

    assert result["stage_timeline"] == [
        {"stage": "deep", "start": "22:30", "end": "23:10", "minutes": 40},
        {"stage": "awake", "start": "23:10", "end": "23:20", "minutes": 10},
    ]
    assert result["awakenings"] == 1
    assert result["spo2"]["lowest"] == 88
    assert result["spo2"]["lowest_at"] == "22:40"
    assert result["spo2"]["minutes_below_90"] == 1
    assert get_sleep_detail(date="02/01/2030")["error"].startswith("invalid date")


def test_activity_segments_totals_and_pause() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_activity_segments(activity_id=1)

    assert result["segment_count"] == 2
    assert result["totals"]["run"] == {"count": 1, "duration": "10:00", "distance_km": 1.6}
    assert result["segments"][0]["pace"] == "6:15/km"
    assert result["paused_time"] == "2:00"


def test_workout_detail_flattens_repeats_and_converts_pace() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_workout_detail(workout_id=7)

    assert "Private Person" not in str(result)
    assert result["steps"][0] == {"type": "warmup", "duration": "10:00", "target": None,
                                  "description": None}
    repeat = result["steps"][1]
    assert repeat["times"] == 5
    assert repeat["steps"][0]["duration"] == "1 km"
    assert repeat["steps"][0]["target"] == {"type": "pace", "range": ["5:00/km", "5:30/km"]}


def test_yearly_totals_convert_units() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    running = get_yearly_totals(year=2030)["sports"][0]

    assert running == {
        "sport": "running",
        "activities": 10,
        "distance_km": 50.0,
        "avg_distance_km": 5.0,
        "longest_km": 10.0,
        "total_hours": 5.0,
    }


def test_without_gps_strips_coordinates() -> None:
    from mcp_garmin_connect.tools import _without_gps

    cleaned = _without_gps({"distance": 5000, "startLatitude": -1.0, "endLongitude": 2.0,
                            "nested": [{"latitude": 1, "hr": 140}]})

    assert cleaned == {"distance": 5000, "nested": [{"hr": 140}]}


def test_tool_schemas_are_objects() -> None:
    for spec in TOOL_SPECS:
        schema = tool_schema(spec)
        assert schema["type"] == "object"
        assert "properties" in schema


def test_get_zones_trims_profile_pii() -> None:
    import mcp_garmin_connect.garmin_client as garmin_client

    garmin_client._manager = FakeManager()

    result = get_zones()

    assert result["available"] is True
    assert "heartRateZones" in result["zones"]
    assert "lactateThresholdHeartRate" in result["zones"]
    assert "gender" not in result["zones"]
    assert "birthDate" not in result["zones"]
    assert "weight" not in result["zones"]


def test_safe_call_records_endpoint_errors() -> None:
    manager = FakeManager()
    errors: list[dict[str, str]] = []

    result = manager.safe_call(
        lambda: (_ for _ in ()).throw(ValueError("bad payload")),
        default=None,
        errors=errors,
        label="sleep:2999-01-01",
    )

    assert result is None
    assert errors == [{"source": "sleep:2999-01-01", "error": "ValueError: bad payload"}]


def test_safe_call_maps_auth_errors() -> None:
    manager = FakeManager()

    class GarminConnectAuthenticationError(Exception):
        pass

    with pytest.raises(GarminAuthError):
        manager.safe_call(lambda: (_ for _ in ()).throw(GarminConnectAuthenticationError()))


class FakeHealthGarmin(FakeGarmin):
    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        # Newest night differs from the oldest, so a wrong pick shows up.
        age = (date.today() - date.fromisoformat(cdate)).days
        return {
            "hrvSummary": {
                "lastNightAvg": [61, 42, 55, 58][age],
                "lastNight5MinHigh": 75,
                "weeklyAvg": 54,
                "baseline": {"lowUpper": 44, "balancedLow": 47, "balancedUpper": 60},
            }
        }

    def get_body_battery(self, start: str, end: str) -> list[dict[str, Any]]:
        # Real response shape: no highest/lowest/end-of-day keys, only the readings.
        return [
            {
                "date": end,
                "charged": 54,
                "drained": 42,
                "bodyBatteryValuesArray": [
                    [1767225600000, 18],
                    [1767254400000, 72],
                    [1767268800000, None],
                    [1767297600000, 30],
                ],
                "endOfDayBodyBatteryDynamicFeedbackEvent": {"bodyBatteryLevel": "VERY_LOW"},
            }
        ]


class FakeStressGarmin(FakeHealthGarmin):
    def get_all_day_stress(self, cdate: str) -> dict[str, Any]:
        # Real dailyStress shape: averages and readings, no duration fields.
        return {
            "calendarDate": cdate,
            "avgStressLevel": 34,
            "maxStressLevel": 88,
            "stressValuesArray": [[1767225600000, 20], [1767225780000, -2]],
        }

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        return {
            "restStressDuration": 27000,
            "lowStressDuration": 21600,
            "mediumStressDuration": 10800,
            "highStressDuration": 1500,
            "activityStressDuration": 5400,
            "bodyBatteryHighestValue": 81,
            "bodyBatteryLowestValue": 9,
            "bodyBatteryMostRecentValue": 27,
        }


def _health_manager(client: FakeGarmin | None = None) -> FakeManager:
    import mcp_garmin_connect.garmin_client as garmin_client

    manager = FakeManager()
    manager._client = client or FakeHealthGarmin()
    garmin_client._manager = manager
    return manager


def test_hrv_history_latest_is_newest_night() -> None:
    _health_manager()

    history = get_hrv_history(days=4)

    assert history["daily"][0]["last_night_avg_ms"] == 61
    assert history["latest_ms"] == 61
    assert history["delta_vs_baseline_ms"] == round(61 - 54.0, 1)


def test_body_battery_range_reads_levels_from_values_array() -> None:
    _health_manager()

    day = get_body_battery_range(days=1)["daily"][0]

    assert day["highest"] == 72
    assert day["lowest"] == 18
    assert day["end_of_day"] == 30


def test_stress_durations_fall_back_to_user_summary() -> None:
    _health_manager(FakeStressGarmin())

    result = get_stress(days=1)
    day = result["daily"][0]

    assert day["stress_avg"] == 34
    assert day["stress_avg_band"] == "low"
    assert day["weekday"] is not None
    assert day["rest_minutes"] == 450
    assert day["activity_minutes"] == 90
    assert day["durations"]["rest"] == "7h 30m"
    assert day["durations"]["high"] == "0h 25m"
    assert result["bands"] == {"rest": "0-25", "low": "26-50", "medium": "51-75", "high": "76-100"}
    assert "errors" not in result


@pytest.mark.parametrize(
    ("value", "band"),
    [(0, "rest"), (25, "rest"), (25.5, "low"), (50, "low"), (51, "medium"), (76, "high"),
     (100, "high"), (-1, None), (None, None)],
)
def test_stress_band(value: Any, band: str | None) -> None:
    assert _stress_band(value) == band


def test_body_battery_range_prefers_user_summary_levels() -> None:
    _health_manager(FakeStressGarmin())

    day = get_body_battery_range(days=1)["daily"][0]

    # The summary is exact; the sparse readings (72/18/30) are only a fallback.
    assert (day["highest"], day["lowest"], day["end_of_day"]) == (81, 9, 27)


def test_hrv_history_reports_personal_baseline_and_bands() -> None:
    _health_manager()

    history = get_hrv_history(days=4)

    assert history["personal_baseline"] == {
        "balanced_low_ms": 47,
        "balanced_high_ms": 60,
        "low_upper_ms": 44,
    }
    assert history["bands"] == {"balanced": "47-60", "unbalanced": "45-46 or >60", "low": "<=44"}
    assert [d["vs_balanced_range"] for d in history["daily"]] == ["above", "below", "in", "in"]
    assert history["daily"][0]["last_night_5min_high_ms"] == 75
    assert history["daily"][0]["weekday"] is not None
