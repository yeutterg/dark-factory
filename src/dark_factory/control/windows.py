"""Dispatch windows evaluated in each window's own timezone."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta

from dark_factory.config import FactoryConfig


def window_open(cfg: FactoryConfig, at: datetime | None = None) -> bool:
    at = at or datetime.now(UTC)
    if not cfg.windows:
        return at.date().isoformat() not in cfg.date_exceptions
    for window in cfg.windows:
        local = at.astimezone(zoneinfo.ZoneInfo(window.timezone))
        if local.date().isoformat() in cfg.date_exceptions:
            continue
        start_h, start_m = map(int, window.start.split(":"))
        end_h, end_m = map(int, window.end.split(":"))
        minutes = local.hour * 60 + local.minute
        start, end = start_h * 60 + start_m, end_h * 60 + end_m
        anchor = local
        if start > end and minutes < end:
            anchor = local - timedelta(days=1)
        if anchor.date().isoformat() in cfg.date_exceptions:
            continue
        inside = (
            start <= minutes < end if start < end else minutes >= start or minutes < end
        )
        if anchor.strftime("%a") in window.days and inside:
            return True
    return False
