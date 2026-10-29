from __future__ import annotations

import inspect
import json

import pytest
from garminconnect import Garmin

from mcp_garmin_connect import extended_tools as ext
from mcp_garmin_connect.config import Settings
from mcp_garmin_connect.garmin_client import reset_manager
from mcp_garmin_connect.tools import TOOL_SPECS


class SignatureCheckingClient:
    """Answers any get_* call, but only if the arguments fit the real Garmin method."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        real = getattr(Garmin, name)

        def call(*args, **kwargs):
            inspect.signature(real).bind(None, *args, **kwargs)
            self.calls.append(name)
            if name == "get_health_snapshots_range":
                return [{"activityUuid": {"uuid": "abc"}, "calendarDate": "2026-10-28"}]
            if name in {"get_ecg", "get_ecg_list"}:
                return {"ecgList": [{"rhythmClassification": "SINUS_RHYTHM",
                                     "samples": list(range(500)), "latitude": 1.0}]}
            return {"value": name, "startLatitude": 1.0, "series": list(range(500))}

        return call


@pytest.fixture
def fake_client(monkeypatch, tmp_path):
    monkeypatch.setenv("GARMIN_TOKEN_STORE", str(tmp_path))
    monkeypatch.delenv("GARMIN_DEMO", raising=False)
    manager = reset_manager(Settings.from_env())
    client = SignatureCheckingClient()
    monkeypatch.setattr(manager, "client", lambda: client)
    yield client
    reset_manager()


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (ext.get_health_snapshots, {"include_epochs": True}),
        (ext.get_ecg_readings, {}),
        (ext.get_ecg_readings, {"ecg_id": "e1"}),
        (ext.get_blood_pressure, {}),
        (ext.get_cardio_respiratory, {}),
        (ext.get_sleep_stats, {}),
        (ext.get_glucose, {}),
        (ext.get_wellness_trends, {}),
        (ext.get_advanced_performance, {}),
        (ext.get_training_plan_context, {}),
        (ext.get_activity_extras, {"activity_id": 123}),
        (ext.get_athlete_profile, {}),
    ],
)
def test_every_call_matches_the_garminconnect_signature(fake_client, tool, kwargs):
    result = tool(**kwargs)
    assert "errors" not in result, result.get("errors")
    assert fake_client.calls


def test_health_snapshot_sessions_are_expanded(fake_client):
    result = ext.get_health_snapshots(include_epochs=True)
    assert result["details"][0]["snapshot_uuid"] == "abc"
    assert "get_health_snapshot_epochs" in fake_client.calls


def test_ecg_waveform_is_omitted_unless_requested(fake_client):
    listed = ext.get_ecg_readings()
    sample = listed["recordings"]["ecgList"][0]
    assert sample["samples"].startswith("<omitted")
    assert "latitude" not in sample

    full = ext.get_ecg_readings(ecg_id="e1", include_waveform=True)
    assert isinstance(full["recording"]["ecgList"][0]["samples"], list)


def test_gps_is_removed_and_long_lists_are_capped(fake_client):
    result = ext.get_glucose()
    readings = result["readings"]
    assert "startLatitude" not in readings
    assert readings["series"][-1] == {"_truncated": "440 more items omitted"}


def test_garmin_read_only_allows_get_methods(fake_client):
    assert "error" in ext.garmin_read("delete_activity", '{"activity_id": 1}')
    assert "error" in ext.garmin_read("get_api_headers")
    assert "error" in ext.garmin_read("download_activity", '{"activity_id": "1"}')
    assert fake_client.calls == []


def test_garmin_read_validates_parameters_before_calling(fake_client):
    bad = ext.garmin_read("get_naps", '{"nope": 1}')
    assert bad["error"].startswith("bad parameters")
    assert "error" in ext.garmin_read("get_naps", "not json")
    assert fake_client.calls == []

    ok = ext.garmin_read("get_naps", json.dumps({"cdate": "2026-10-29"}))
    assert ok["result"]["value"] == "get_naps"


def test_list_garmin_methods_covers_health_snapshot_and_ecg():
    names = {m["method"] for m in ext.list_garmin_methods()["methods"]}
    assert {"get_health_snapshots", "get_ecg_list", "get_blood_pressure_day"} <= names
    assert not any(n.startswith(("set_", "delete_", "upload_", "add_")) for n in names)
    assert "get_api_headers" not in names
    assert ext.list_garmin_methods("ecg")["count"] >= 3


def test_extended_tools_are_registered():
    names = {spec.name for spec in TOOL_SPECS}
    assert {spec.name for spec in ext.EXTENDED_TOOL_SPECS} <= names
    assert len(names) == len(TOOL_SPECS), "tool names must be unique"


def test_one_rejected_endpoint_does_not_sink_the_tool(fake_client, monkeypatch):
    from mcp_garmin_connect.errors import GarminConnectionError

    original = fake_client.__class__.__getattr__

    def flaky(self, name):
        if name == "get_running_economy":
            def reject(*_a, **_k):
                raise GarminConnectionError("API Error 400 - Invalid activity type")
            return reject
        return original(self, name)

    monkeypatch.setattr(fake_client.__class__, "__getattr__", flaky)
    result = ext.get_advanced_performance()
    assert result["running_economy"] is None
    assert any(e["source"] == "get_running_economy" for e in result["errors"])
    assert result["current_biometrics"] is not None
