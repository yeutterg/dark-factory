"""Packaged, dependency-free browser dashboard assets."""

from __future__ import annotations

from importlib.resources import files


def static_asset(name: str) -> bytes:
    if name not in {"login.html", "index.html", "app.css", "app.js"}:
        raise ValueError("unknown static asset")
    return files("dark_factory.ui.static").joinpath(name).read_bytes()


LOGIN_HTML = static_asset("login.html").decode()


def dashboard_html() -> str:
    return static_asset("index.html").decode()
