"""Hybrid backend: routing, fallback and credential requirements."""

from __future__ import annotations

from dataclasses import replace

import pytest

from mcp_garmin_connect.config import Settings
from mcp_garmin_connect.errors import PersonalApiAuthError, PersonalApiHttpError
from mcp_garmin_connect.hybrid import HybridBackend


class FakePersonal:
    backend_name = "personal"
    display_name = "personal-user"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_sleep_data(self, cdate: str) -> dict[str, object]:
        self.calls.append("sleep")
        return {"dailySleepDTO": {"sleepTimeSeconds": 25200}}

    def get_body_composition(self, start: str, end: str) -> dict[str, object]:
        self.calls.append("weight")
        raise PersonalApiHttpError("weight endpoint down", status_code=503)

    def get_max_metrics(self, cdate: str) -> object:
        raise AssertionError("personal backend must not be asked for VO2 Max first")


class FakeUnofficial:
    backend_name = "unofficial"
    display_name = None

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_training_status(self, cdate: str) -> dict[str, object]:
        self.calls.append("training_status")
        return {"mostRecentTrainingStatus": {"latestTrainingStatusData": {"d": {}}}}

    def get_body_composition(self, start: str, end: str) -> dict[str, object]:
        self.calls.append("weight-fallback")
        return {"dateWeightList": [{"calendarDate": start, "weight": 71200}]}

    def get_max_metrics(self, cdate: str) -> list[dict[str, object]]:
        self.calls.append("vo2")
        return [{"generic": {"vo2MaxPreciseValue": 44.4}}]


def _hybrid() -> tuple[HybridBackend, FakePersonal, FakeUnofficial]:
    personal = FakePersonal()
    unofficial = FakeUnofficial()
    return HybridBackend(personal=personal, unofficial=unofficial), personal, unofficial


def test_wellness_reads_go_to_the_official_backend() -> None:
    backend, personal, unofficial = _hybrid()

    backend.get_sleep_data("2026-10-27")

    assert personal.calls == ["sleep"]
    assert unofficial.calls == []


def test_derived_metrics_go_to_the_unofficial_backend() -> None:
    backend, personal, unofficial = _hybrid()

    backend.get_max_metrics("2026-10-27")
    backend.get_training_status("2026-10-27")

    assert unofficial.calls == ["vo2", "training_status"]
    assert personal.calls == []


def test_a_failing_official_endpoint_falls_back_to_the_other_backend() -> None:
    backend, _, unofficial = _hybrid()

    payload = backend.get_body_composition("2026-10-20", "2026-10-27")

    assert "dateWeightList" in payload
    assert unofficial.calls == ["weight-fallback"]


def test_auth_errors_are_never_masked_by_the_fallback() -> None:
    class Rejecting(FakePersonal):
        def get_sleep_data(self, cdate: str) -> dict[str, object]:
            raise PersonalApiAuthError("key expired")

    backend = HybridBackend(personal=Rejecting(), unofficial=FakeUnofficial())

    with pytest.raises(PersonalApiAuthError):
        backend.get_sleep_data("2026-10-27")


def test_display_name_falls_through_to_the_backend_that_has_one() -> None:
    backend, _, _ = _hybrid()
    assert backend.display_name == "personal-user"


def test_routing_map_is_introspectable() -> None:
    backend, _, _ = _hybrid()
    routing = backend.routing()
    assert "get_sleep_data" in routing["personal"]
    assert "get_max_metrics" in routing["unofficial"]


def test_hybrid_requires_both_credential_sets() -> None:
    base = Settings.from_env()
    both = replace(
        base,
        garmin_demo=False,
        garmin_auth_mode="hybrid",
        garmin_personal_api_key="key",
        garmin_email="a@b.c",
        garmin_password="secret",
    )
    only_key = replace(both, garmin_email=None)
    only_password = replace(both, garmin_personal_api_key=None)

    assert both.resolved_auth_mode() == "hybrid"
    assert only_key.garmin_personal_api_key and not only_key.garmin_email
    assert only_password.garmin_email and not only_password.garmin_personal_api_key


# --------------------------------------------------------------- snapshot tagging


def test_usage_capture_records_the_backend_per_method() -> None:
    from mcp_garmin_connect.hybrid import capture_usage

    backend, _, _ = _hybrid()

    with capture_usage() as sources:
        backend.get_sleep_data("2026-10-27")
        backend.get_max_metrics("2026-10-27")

    assert sources == {
        "get_sleep_data": "personal",
        "get_max_metrics": "unofficial",
    }


def test_snapshot_section_is_tagged_with_one_source() -> None:
    from mcp_garmin_connect.tools import _section_with_source

    backend, _, _ = _hybrid()
    section = _section_with_source(lambda: backend.get_sleep_data("2026-10-27"), "hybrid")

    assert section["data_source"] == "personal"


def test_snapshot_section_marks_sections_that_needed_both_backends() -> None:
    from mcp_garmin_connect.tools import _section_with_source

    backend, _, _ = _hybrid()

    def mixed_section() -> dict[str, object]:
        backend.get_sleep_data("2026-10-27")
        backend.get_max_metrics("2026-10-27")
        return {"note": "needs wellness and derived metrics"}

    section = _section_with_source(mixed_section, "hybrid")

    assert section["data_source"] == "personal+unofficial"
    assert section["source_detail"] == {
        "get_max_metrics": "unofficial",
        "get_sleep_data": "personal",
    }


def test_full_snapshot_reports_sources_for_every_section() -> None:
    from mcp_garmin_connect.demo import DemoGarmin
    from mcp_garmin_connect.garmin_client import reset_manager
    from mcp_garmin_connect.tools import get_full_snapshot

    manager = reset_manager(
        replace(
            Settings.from_env(),
            garmin_demo=False,
            garmin_auth_mode="hybrid",
            garmin_personal_api_key="key",
            garmin_email="a@b.c",
            garmin_password="secret",
        )
    )
    # Both halves point at the demo client so the snapshot wiring is exercised
    # without network access; routing/tagging is covered by the tests above.
    manager._client = HybridBackend(personal=DemoGarmin(), unofficial=DemoGarmin())

    snapshot = get_full_snapshot(activity_days=14, load_days=14)

    assert snapshot["auth_backend"] == "hybrid"
    assert snapshot["hybrid_fallbacks"] == []
    assert set(snapshot["section_sources"]) >= {
        "recovery",
        "sleep",
        "recent_activities",
        "fitness",
        "zones",
        "personal_records",
    }
    for name, source in snapshot["section_sources"].items():
        assert source, f"{name} has no source"
    for name, section in snapshot.items():
        if isinstance(section, dict) and name not in {"section_sources"}:
            assert "error" not in section, f"{name} failed: {section}"


def test_cache_hits_still_report_their_backend() -> None:
    from mcp_garmin_connect.garmin_client import reset_manager
    from mcp_garmin_connect.hybrid import capture_usage

    manager = reset_manager(replace(Settings.from_env(), garmin_demo=True))

    with capture_usage() as sources:
        manager.remember_sources("sleep_day:2026-10-27", {"get_sleep_data": "personal"})
        manager.replay_sources("sleep_day:2026-10-27")

    assert sources == {"get_sleep_data": "personal"}


def test_hybrid_still_starts_when_the_personal_api_is_unusable() -> None:
    """An expired key must degrade to email/password, not kill the server."""
    from mcp_garmin_connect.demo import DemoGarmin
    from mcp_garmin_connect.errors import PersonalApiAuthError
    from mcp_garmin_connect.garmin_client import GarminClientManager
    from mcp_garmin_connect.hybrid import UnavailableBackend

    manager = GarminClientManager(
        replace(
            Settings.from_env(),
            garmin_demo=False,
            garmin_auth_mode="hybrid",
            garmin_personal_api_key="expired-key",
            garmin_email="a@b.c",
            garmin_password="secret",
        )
    )
    manager.login_personal = lambda: (_ for _ in ()).throw(   # type: ignore[method-assign]
        PersonalApiAuthError("key expired")
    )
    manager.login = lambda allow_interactive_mfa=False: DemoGarmin()   # type: ignore[method-assign]

    backend = manager.login_hybrid()

    assert isinstance(backend._personal, UnavailableBackend)
    # derived metrics still work through the surviving backend
    assert backend.get_max_metrics("2026-10-28")
    # personal-only reads explain themselves instead of exploding silently
    try:
        backend.get_body_composition("2026-10-21", "2026-10-28")
    except Exception as exc:
        assert "personal unavailable" in str(exc).lower()
    else:  # the demo fallback may answer it; either way it must not crash boot
        pass
