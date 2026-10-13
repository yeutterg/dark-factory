"""Resolve Together credentials from the host Pi login without printing them."""

from __future__ import annotations

from dark_factory.worker import _route_secret


def test_together_secret_uses_pi_login(monkeypatch):
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)

    def fake_run(*args, **kwargs):
        class Result:
            returncode = 0
            stdout = "together-test-key\n"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    secret = _route_secret(
        {
            "provider": "together",
            "auth_env": "TOGETHER_API_KEY",
            "harness": "pi",
        }
    )
    assert secret == "together-test-key"


def test_together_secret_prefers_environment(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "from-env")

    def boom(*args, **kwargs):
        raise AssertionError("must not print a Pi credential when the env is set")

    monkeypatch.setattr("subprocess.run", boom)
    assert (
        _route_secret({"provider": "together", "auth_env": "TOGETHER_API_KEY"})
        == "from-env"
    )
