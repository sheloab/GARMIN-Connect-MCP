from __future__ import annotations

from dataclasses import replace
from typing import Any

from mcp_garmin_connect.config import Settings
from mcp_garmin_connect.garmin_client import reset_manager
from mcp_garmin_connect.tools import (
    get_body_battery_range,
    get_fitness,
    get_full_snapshot,
    get_health_summary,
    get_hrv_history,
    get_recent_activities,
    get_running_summary,
    get_zones,
)


def _demo_settings() -> Settings:
    return replace(Settings.from_env(), garmin_demo=True)


def test_demo_recovery_and_health_summary() -> None:
    reset_manager(_demo_settings())

    summary = get_health_summary(days=3)

    recovery = summary["recovery"]
    assert recovery["training_readiness"]["score"] == 78
    assert recovery["resting_heart_rate_bpm"] == 58
    assert recovery["hrv"]["weekly_avg_ms"] == 44.0
    assert "errors" not in recovery
    assert "errors" not in summary["sleep"]


def test_demo_running_tools() -> None:
    reset_manager(_demo_settings())

    activities = get_recent_activities(days=30)
    assert activities["count"] == 6

    summary = get_running_summary(days=30)
    assert summary["sessions"] == 6
    assert summary["total_distance_km"] == 42.7
    assert summary["fastest_run"]["name"] == "Tempo Run"


def test_demo_fitness_and_zones() -> None:
    reset_manager(_demo_settings())

    fitness = get_fitness()
    assert fitness["vo2_max_running"] == 47.0
    assert fitness["race_predictions"]["5k"]["seconds"] == 1660

    zones = get_zones()
    assert zones["available"] is True
    assert "heartRateZones" in zones["zones"]


def test_demo_full_snapshot_has_no_errors() -> None:
    reset_manager(_demo_settings())

    snapshot = get_full_snapshot(activity_days=14, load_days=14)

    for name, section in snapshot.items():
        if isinstance(section, dict):
            assert "error" not in section, f"{name} failed: {section}"
            assert "errors" not in section, f"{name} partial errors: {section}"

    assert snapshot["recent_activities"]["count"] == 6
    assert snapshot["personal_records"]["count"] == 4


def test_demo_hrv_history() -> None:
    reset_manager(_demo_settings())

    history = get_hrv_history(days=7)

    assert history["period_days"] == 7
    assert history["days_with_data"] == 7
    assert history["latest_ms"] == 44
    assert history["baseline_avg_ms"] == 44.0
    assert history["delta_vs_baseline_ms"] == 0.0
    assert history["daily"][0]["status"] == "BALANCED"
    assert "errors" not in history


def test_demo_body_battery_range() -> None:
    reset_manager(_demo_settings())

    battery = get_body_battery_range(days=5)

    assert battery["days_with_data"] == 5
    assert battery["daily"][0]["charged"] == 62
    assert battery["daily"][0]["end_of_day"] == 37
    # Seperti tool lain: daily terbaru-dulu, dan tiap entri punya tanggal.
    dates = [entry["date"] for entry in battery["daily"]]
    assert dates == sorted(dates, reverse=True)
    assert all(dates)
    assert "errors" not in battery


def test_history_tools_respect_the_health_cap(monkeypatch: Any) -> None:
    import mcp_garmin_connect.tools as tools

    reset_manager(_demo_settings())
    monkeypatch.setattr(tools, "MAX_DAYS_HEALTH", 14)

    assert tools.get_hrv_history(days=365)["period_days"] == 14
    assert tools.get_body_battery_range(days=365)["period_days"] == 14
