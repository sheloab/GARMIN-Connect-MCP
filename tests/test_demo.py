from __future__ import annotations

from dataclasses import replace

from mcp_garmin_connect.config import Settings
from mcp_garmin_connect.garmin_client import reset_manager
from mcp_garmin_connect.tools import (
    get_fitness,
    get_full_snapshot,
    get_health_summary,
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
