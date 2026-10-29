from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from mcp_garmin_connect import coach
from mcp_garmin_connect.config import Settings
from mcp_garmin_connect.garmin_client import reset_manager

TODAY = date(2026, 10, 29)


def _run(days_ago: int, km: float, hour: int = 6, lat: float = 52.5163, lon: float = 13.3777):
    day = TODAY - timedelta(days=days_ago)
    return {
        "activityType": {"typeKey": "running"},
        "startTimeLocal": f"{day.isoformat()} {hour:02d}:10:00",
        "distance": km * 1000,
        "duration": km * 360,
        "startLatitude": lat,
        "startLongitude": lon,
    }


@pytest.fixture
def athlete(monkeypatch, tmp_path):
    monkeypatch.setenv("GARMIN_TOKEN_STORE", str(tmp_path))
    monkeypatch.delenv("GARMIN_DEMO", raising=False)
    monkeypatch.delenv("AIR_QUALITY_ENABLED", raising=False)
    monkeypatch.delenv("AIR_QUALITY_GRID_DEG", raising=False)
    reset_manager(Settings.from_env())
    monkeypatch.setattr(coach, "today_iso", lambda: TODAY.isoformat())
    # ~30 km/week, long run 14 km, mostly in one area plus a few runs elsewhere.
    runs = []
    for week in range(12):
        base = week * 7
        runs += [_run(base + 1, 8), _run(base + 3, 8), _run(base + 6, 14 if week < 4 else 12)]
    runs += [_run(2, 6, lat=52.4, lon=13.5)]
    monkeypatch.setattr(coach, "_activities_paged", lambda *a, **k: runs)
    monkeypatch.setattr(coach, "get_fitness", lambda: {
        "race_predictions": {"10k": {"seconds": 3000}, "half_marathon": {"seconds": 6600},
                             "marathon": {"seconds": 14100}}})
    monkeypatch.setattr(coach, "get_zones", lambda: {"heart_rate_zones": [{"sport": "RUNNING"}]})
    monkeypatch.setattr(coach, "get_recovery", lambda: {
        "training_readiness": {"score": 70}, "hrv": {"status_latest": "BALANCED"},
        "training_status": "PRODUCTIVE"})
    yield runs
    reset_manager()


def test_vdot_matches_daniels_reference():
    # Daniels' tables: a 50:00 10K is VDOT ~40, threshold ~5:06/km.
    assert round(coach.vdot_from_race(10_000, 3000)) == 40
    paces = coach.training_paces(40)
    assert paces["threshold"].startswith("5:0")


def test_weeks_zero_returns_a_recommendation_without_a_plan(athlete):
    result = coach.build_running_plan("half_marathon")
    assert result["plan_length"]["recommended_weeks"] in coach._PLAN_WEEKS
    assert result["plan_length"]["choices"] == [2, 4, 8, 12, 16]
    assert "weeks" not in result
    assert result["history"]["avg_weekly_km_4w"] > 20


@pytest.mark.parametrize("goal", sorted(coach.GOALS))
@pytest.mark.parametrize("weeks", coach._PLAN_WEEKS)
def test_plans_follow_the_evidence_rules(athlete, goal, weeks):
    plan = coach.build_running_plan(goal, weeks=weeks, days_per_week=5)
    assert len(plan["weeks"]) == weeks
    assert plan["weeks"][-1]["phase"] == "taper"
    assert plan["weeks"][-1]["sessions"][-1]["type"] == "race"

    longest = plan["history"]["longest_run_30d_km"]
    volumes = [w["target_km"] for w in plan["weeks"]]
    for week in plan["weeks"]:
        long_runs = [s["km"] for s in week["sessions"] if s["type"] == "long_run"]
        for km in long_runs:
            assert km <= round(longest * 1.10, 1) + 0.05, "single-session spike > 10%"
            longest = max(longest, km)
        quality = [s for s in week["sessions"] if s["type"] not in {"easy", "long_run", "race"}]
        assert len(quality) <= 2
    full_weeks = [w["target_km"] for w in plan["weeks"]
                  if not w["step_back_week"] and w["phase"] != "taper"]
    for prev, cur in zip(full_weeks, full_weeks[1:], strict=False):
        assert cur <= prev * 1.10 + 0.1, "full-week volume rose more than 10%"
    # Race week is excluded: its total includes the race itself.
    assert max(volumes[:-1] or [0]) <= (
        max(plan["history"]["avg_weekly_km_4w"], 10) * 1.10 ** weeks + 0.1
    )
    assert {e["id"] for e in plan["evidence"]} >= {"single_session_spike", "taper"}
    assert plan["training_paces"]["easy"]


def test_short_plan_for_marathon_warns(athlete):
    plan = coach.build_running_plan("marathon", weeks=4)
    assert any("shorter than the recommended" in w for w in plan["warnings"])


def test_bad_inputs_are_rejected(athlete):
    assert "error" in coach.build_running_plan("ultra")
    assert "error" in coach.build_running_plan("10k", weeks=6)
    assert "error" in coach.build_running_plan("10k", race_date="next week")


def _fake_open_meteo(captured):
    def fetch(params):
        captured.append(params)
        start = date.fromisoformat(params.get("start_date", TODAY.isoformat()))
        end = date.fromisoformat(params.get("end_date", (TODAY + timedelta(days=2)).isoformat()))
        times, aqi = [], []
        day = start
        while day <= end:
            for hour in range(24):
                times.append(f"{day.isoformat()}T{hour:02d}:00")
                aqi.append(40 if hour < 8 else 120)
            day += timedelta(days=1)
        n = len(times)
        pm = [8.0 if t[11:13] < "08" else 30.0 for t in times]
        return {"hourly": {"time": times, "us_aqi": aqi, "pm2_5": pm,
                           "pm10": [20.0] * n, "ozone": [50.0] * n,
                           "nitrogen_dioxide": [15.0] * n}}
    return fetch


def test_air_quality_uses_rounded_areas_only(athlete, monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(coach, "_fetch_air_quality", _fake_open_meteo(captured))
    result = coach.get_running_air_quality(days=90, forecast_days=2)

    top = result["areas"][0]
    assert top["grid_point"] == {"lat": 52.52, "lon": 13.38, "grid_deg": 0.01}
    assert top["share_of_runs_pct"] > 90
    assert top["during_your_runs"]["median_us_aqi"] == 40
    assert top["forecast"][0]["best_hours"][0]["hour"] < 8
    assert top["cleanest_hours_historically"][0]["hour"] < 8
    assert top["forecast"] and top["forecast"][0]["hours_over_100"]
    sent = json.dumps(captured)
    assert "52.5163" not in sent and "13.3777" not in sent, "exact GPS must not leave"
    assert all(p["latitude"] in (52.52, 52.4) for p in captured)


def test_air_quality_can_be_disabled(athlete, monkeypatch):
    monkeypatch.setenv("AIR_QUALITY_ENABLED", "0")
    reset_manager(Settings.from_env())
    assert "error" in coach.get_running_air_quality()


def test_air_quality_failure_is_reported_not_raised(athlete, monkeypatch):
    def boom(_params):
        raise OSError("network down")

    monkeypatch.setattr(coach, "_fetch_air_quality", boom)
    result = coach.get_running_air_quality(forecast_days=0)
    assert result["errors"][0]["source"] == "open_meteo_air_quality"
