from __future__ import annotations

import pytest

from mcp_garmin_connect.config import Settings
from mcp_garmin_connect.errors import GarminRateLimitError
from mcp_garmin_connect.garmin_client import GarminClientManager


class GarminConnectTooManyRequestsError(Exception):
    """Same name as the garminconnect exception, so the manager maps it to a rate limit."""


class FakeGarmin:
    def __init__(self, calls: list[str], fail: bool) -> None:
        self.calls = calls
        self.fail = fail

    def login(self, _token_store: str) -> None:
        self.calls.append("login")
        if self.fail:
            raise GarminConnectTooManyRequestsError("Mobile login returned 429")


def _manager(monkeypatch, tmp_path, *, cooldown: int = 900, mode: str = "unofficial"):
    monkeypatch.setenv("GARMIN_EMAIL", "runner@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("GARMIN_TOKEN_STORE", str(tmp_path))
    monkeypatch.setenv("GARMIN_AUTH_MODE", mode)
    monkeypatch.setenv("GARMIN_LOGIN_COOLDOWN_SECONDS", str(cooldown))
    monkeypatch.delenv("GARMIN_DEMO", raising=False)
    monkeypatch.delenv("GARMIN_PERSONAL_API_KEY", raising=False)
    manager = GarminClientManager(Settings.from_env())
    calls: list[str] = []
    state = {"fail": True}
    monkeypatch.setattr(
        manager, "build_client", lambda allow_interactive_mfa: FakeGarmin(calls, state["fail"])
    )
    return manager, calls, state


def test_failed_login_pauses_further_attempts(monkeypatch, tmp_path):
    manager, calls, _ = _manager(monkeypatch, tmp_path)

    with pytest.raises(GarminRateLimitError):
        manager.client()
    assert calls == ["login"]
    assert (tmp_path / "login_cooldown.json").is_file()

    for _ in range(5):
        with pytest.raises(GarminRateLimitError, match="login paused until"):
            manager.client()
    assert calls == ["login"], "tool calls must not reach Garmin during the cooldown"


def test_cooldown_is_shared_between_server_processes(monkeypatch, tmp_path):
    first, first_calls, _ = _manager(monkeypatch, tmp_path)
    with pytest.raises(GarminRateLimitError):
        first.client()

    second, second_calls, _ = _manager(monkeypatch, tmp_path)
    with pytest.raises(GarminRateLimitError, match="login paused until"):
        second.client()
    assert second_calls == []


def test_interactive_login_bypasses_cooldown_and_clears_it(monkeypatch, tmp_path):
    manager, calls, state = _manager(monkeypatch, tmp_path)
    with pytest.raises(GarminRateLimitError):
        manager.client()

    state["fail"] = False
    manager.login(allow_interactive_mfa=True)
    assert calls == ["login", "login"]
    assert not (tmp_path / "login_cooldown.json").exists()
    assert manager.client() is not None


def test_zero_cooldown_disables_the_pause(monkeypatch, tmp_path):
    manager, calls, _ = _manager(monkeypatch, tmp_path, cooldown=0)
    for _ in range(3):
        with pytest.raises(GarminRateLimitError):
            manager.client()
    assert calls == ["login", "login", "login"]
    assert not (tmp_path / "login_cooldown.json").exists()


def test_expired_cooldown_allows_a_new_attempt(monkeypatch, tmp_path):
    manager, calls, _ = _manager(monkeypatch, tmp_path)
    with pytest.raises(GarminRateLimitError):
        manager.client()
    (tmp_path / "login_cooldown.json").write_text('{"until": 1, "reason": "old"}', encoding="utf-8")
    with pytest.raises(GarminRateLimitError):
        manager.client()
    assert calls == ["login", "login"]


def test_hybrid_keeps_personal_api_when_email_login_fails(monkeypatch, tmp_path):
    manager, calls, _ = _manager(monkeypatch, tmp_path, mode="hybrid")
    personal = object()
    monkeypatch.setattr(manager, "login_personal", lambda: personal)

    client = manager.client()
    assert client is manager.client(), "degraded hybrid client is reused during the cooldown"
    assert calls == ["login"]
    assert manager._rebuild_after is not None
