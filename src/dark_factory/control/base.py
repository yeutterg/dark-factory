"""Shared controller primitives: errors, transactions and stable digests."""

from __future__ import annotations

import hashlib
import json
from functools import wraps
from typing import Any


class ControllerError(RuntimeError):
    pass


def atomic(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.store.transaction(), self.decision():
            return method(self, *args, **kwargs)

    return wrapped


def decided(method):
    """Evaluate a read-only view against one consistent policy snapshot."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.decision():
            return method(self, *args, **kwargs)

    return wrapped


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
