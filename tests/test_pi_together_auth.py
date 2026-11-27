"""A route's credential comes only from its named reference, never a host login."""

from __future__ import annotations

from dark_factory.worker import _route_secret


def test_route_secret_comes_from_its_named_environment(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "from-env")
    monkeypatch.delenv("TOGETHER_API_KEY_FILE", raising=False)
    route = {"provider": "together", "auth_env": "TOGETHER_API_KEY", "harness": "pi"}
    assert _route_secret(route) == "from-env"


def test_route_secret_can_come_from_a_mounted_file(monkeypatch, tmp_path):
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    path = tmp_path / "together"
    path.write_text("from-file\n")
    monkeypatch.setenv("TOGETHER_API_KEY_FILE", str(path))
    route = {"provider": "together", "auth_env": "TOGETHER_API_KEY", "harness": "pi"}
    assert _route_secret(route) == "from-file"


def test_no_host_login_is_ever_consulted(monkeypatch):
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    monkeypatch.delenv("TOGETHER_API_KEY_FILE", raising=False)
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("login"))
    )
    route = {"provider": "together", "auth_env": "TOGETHER_API_KEY", "harness": "pi"}
    assert _route_secret(route) == ""
