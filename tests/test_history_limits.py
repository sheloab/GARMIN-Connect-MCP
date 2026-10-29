"""Regression tests for issue #3 (recovery unit) and the env-tunable history caps.

Covers:
* `recovery_time_hours` must convert Garmin's minutes to hours (issue #3, @sheloab).
* Activity history must page through Garmin instead of silently stopping at 200.
* The tool caps (30/365/200) are configuration, not hardcoded numbers.
"""

from __future__ import annotations

from typing import Any

from mcp_garmin_connect.config import (
    MAX_ACTIVITY_LIMIT,
    MAX_DAYS_ACTIVITIES,
    MAX_DAYS_HEALTH,
    Settings,
)
from mcp_garmin_connect.garmin_client import GarminClientManager, reset_manager
from mcp_garmin_connect.tools import TOOL_SPECS, _activities_paged, _minutes_to_hours

# --------------------------------------------------------------- issue #3


def test_recovery_time_is_converted_from_minutes_to_hours() -> None:
    """Values reported in issue #3: 793, 1082, 660, 1 minutes."""
    assert _minutes_to_hours(793) == 13.2
    assert _minutes_to_hours(1082) == 18.0
    assert _minutes_to_hours(660) == 11.0
    assert _minutes_to_hours(1) == 0.0


def test_recovery_time_handles_missing_and_non_numeric_values() -> None:
    assert _minutes_to_hours(None) is None
    assert _minutes_to_hours("not a number") is None
    # bool is a subclass of int in Python and must not be treated as a duration.
    assert _minutes_to_hours(True) is None
    assert _minutes_to_hours(False) is None


# --------------------------------------------------------------- pagination


class FakeGarmin:
    def __init__(self, total: int) -> None:
        self.total = total
        self.calls: list[tuple[int, int]] = []

    def get_activities(self, start: int, limit: int) -> list[dict[str, Any]]:
        self.calls.append((start, limit))
        return [
            {
                "activityId": index + 1,
                "activityName": "Run",
                "activityType": {"typeKey": "running"},
                "startTimeLocal": "2999-01-01 07:00:00",
                "duration": 1800,
                "distance": 5000,
                "averageHR": 140,
            }
            for index in range(start, min(start + limit, self.total))
        ]


class FakeManager(GarminClientManager):
    def __init__(self, total: int = 0) -> None:
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
        self._client = FakeGarmin(total)


def _install(total: int) -> FakeGarmin:
    reset_manager()
    import mcp_garmin_connect.garmin_client as garmin_client

    manager = FakeManager(total)
    garmin_client._manager = manager
    return manager._client  # type: ignore[return-value]


def test_activities_paged_walks_every_page() -> None:
    fake = _install(450)

    activities = _activities_paged(450)

    assert len(activities) == 450
    assert fake.calls == [(0, 200), (200, 200), (400, 200)]


def test_activities_paged_stops_at_max_items() -> None:
    fake = _install(1000)

    activities = _activities_paged(250)

    assert len(activities) == 250
    # Only two pages are needed for 250 items.
    assert fake.calls == [(0, 200), (200, 200)]


def test_activities_paged_handles_fewer_items_than_one_page() -> None:
    fake = _install(12)

    activities = _activities_paged(500)

    assert len(activities) == 12
    assert fake.calls == [(0, 200)]


def test_activities_paged_with_zero_budget_makes_no_call() -> None:
    fake = _install(100)

    assert _activities_paged(0) == []
    assert fake.calls == []


# --------------------------------------------------------------- caps


def test_caps_fall_back_to_the_documented_defaults(monkeypatch: Any) -> None:
    """The documented defaults apply when the env vars are unset (this repo's .env may set them)."""
    from mcp_garmin_connect.config import _env_int_min

    monkeypatch.delenv("GARMIN_MAX_DAYS_HEALTH", raising=False)
    assert _env_int_min("GARMIN_MAX_DAYS_HEALTH", 30) == 30

    monkeypatch.setenv("GARMIN_MAX_DAYS_HEALTH", "90")
    assert _env_int_min("GARMIN_MAX_DAYS_HEALTH", 30) == 90

    # A non-numeric or non-positive value must not disable the cap.
    monkeypatch.setenv("GARMIN_MAX_DAYS_HEALTH", "not-a-number")
    assert _env_int_min("GARMIN_MAX_DAYS_HEALTH", 30) == 30
    monkeypatch.setenv("GARMIN_MAX_DAYS_HEALTH", "0")
    assert _env_int_min("GARMIN_MAX_DAYS_HEALTH", 30) == 1


def test_tool_specs_share_the_configured_caps() -> None:
    """Changing the env vars must move the tool schema too, not just the clamp."""
    spec_by_name = {spec.name: spec for spec in TOOL_SPECS}

    assert spec_by_name["get_sleep"].parameters["days"]["maximum"] == MAX_DAYS_HEALTH
    assert spec_by_name["get_stress"].parameters["days"]["maximum"] == MAX_DAYS_HEALTH
    assert spec_by_name["get_health_summary"].parameters["days"]["maximum"] == MAX_DAYS_HEALTH
    assert (
        spec_by_name["get_recent_activities"].parameters["days"]["maximum"] == MAX_DAYS_ACTIVITIES
    )
    assert (
        spec_by_name["get_recent_activities"].parameters["limit"]["maximum"] == MAX_ACTIVITY_LIMIT
    )
    assert (
        spec_by_name["get_full_snapshot"].parameters["activity_days"]["maximum"]
        == MAX_DAYS_ACTIVITIES
    )


# --------------------------------------------------------------- unit audit


def test_personal_record_value_unit_is_derived_from_the_label() -> None:
    from mcp_garmin_connect.tools import _personal_record_value_unit

    # Durations come back as "h:mm:ss" strings; the rest are numbers with different units.
    assert _personal_record_value_unit("Fastest 5K") == "duration"
    assert _personal_record_value_unit("Fastest 40K ride") == "duration"
    assert _personal_record_value_unit("Best 20-minute average power") == "watts"
    assert _personal_record_value_unit("Longest run") == "distance_m"
    assert _personal_record_value_unit("Longest ride") == "distance_m"
    assert _personal_record_value_unit("Most elevation gained") == "elevation_m"
    assert _personal_record_value_unit("Most steps in a day") == "steps"
    assert _personal_record_value_unit("Longest step goal streak") == "days"
    assert _personal_record_value_unit(None) is None
    assert _personal_record_value_unit("Something new from Garmin") is None


def test_health_summary_respects_the_configured_health_cap(monkeypatch: Any) -> None:
    """It used to clamp RHR at a hardcoded 30 even when the cap was raised."""
    import mcp_garmin_connect.tools as tools

    monkeypatch.setattr(tools, "MAX_DAYS_HEALTH", 90)
    _install(0)

    summary = tools.get_health_summary(90)

    assert summary["resting_heart_rate"]["period_days"] == 90


# --------------------------------------------------------------- explicit windows


class DatedGarmin:
    """Activities newest-first, like Garmin, so window/paging logic can be exercised."""

    def __init__(self, dates: list[str]) -> None:
        self.dates = dates
        self.calls: list[tuple[int, int]] = []

    def get_activities(self, start: int, limit: int) -> list[dict[str, Any]]:
        self.calls.append((start, limit))
        return [
            {
                "activityId": index + 1,
                "activityName": "Run",
                "activityType": {"typeKey": "running"},
                "startTimeLocal": f"{day} 07:00:00",
                "duration": 1800,
                "distance": 5000,
                "averageHR": 140,
            }
            for index, day in enumerate(self.dates[start : start + limit], start=start)
        ]


def _install_dated(dates: list[str]) -> DatedGarmin:
    reset_manager()
    import mcp_garmin_connect.garmin_client as garmin_client

    class Manager(FakeManager):
        def __init__(self) -> None:
            super().__init__(0)
            self._client = DatedGarmin(dates)

    manager = Manager()
    garmin_client._manager = manager
    return manager._client  # type: ignore[return-value]


def _days_desc(count: int, newest: str = "2026-10-16") -> list[str]:
    from datetime import date, timedelta

    start = date.fromisoformat(newest)
    return [(start - timedelta(days=offset)).isoformat() for offset in range(count)]


def test_explicit_window_filters_to_the_requested_dates() -> None:
    from mcp_garmin_connect.tools import get_recent_activities

    _install_dated(_days_desc(400))

    result = get_recent_activities(start_date="2026-08-01", end_date="2026-08-31", limit=1000)

    assert result["window"] == {"start": "2026-08-01", "end": "2026-08-31"}
    days = sorted(a["date"][:10] for a in result["activities"])
    assert days[0] >= "2026-08-01" and days[-1] <= "2026-08-31"
    assert len(days) == 31


def test_paging_stops_once_it_reaches_the_window_start() -> None:
    from mcp_garmin_connect.tools import get_recent_activities

    fake = _install_dated(_days_desc(500))

    get_recent_activities(start_date="2026-06-01", limit=1000)

    # 500 activities are one page short of a full second page; the loop must stop
    # rather than keep walking pages for a window it already reached.
    assert len(fake.calls) <= 3
    assert fake.calls[0] == (0, 200)


def test_invalid_dates_are_rejected_without_calling_garmin() -> None:
    from mcp_garmin_connect.tools import get_recent_activities

    fake = _install_dated(_days_desc(10))

    assert "error" in get_recent_activities(start_date="01-08-2026")
    assert "error" in get_recent_activities(end_date="besok")
    assert "error" in get_recent_activities(start_date="2026-09-01", end_date="2026-08-01")
    assert fake.calls == []


def test_days_still_works_when_no_dates_are_given() -> None:
    from mcp_garmin_connect.tools import get_recent_activities

    _install_dated(_days_desc(40))

    result = get_recent_activities(days=7)

    assert "window" not in result
    assert result["count"] <= 7
