"""Read a credential from the environment or from a mounted secret file.

``NAME`` holds the value directly. ``NAME_FILE`` names a file that holds it,
which is how container secrets are delivered: the value never sits in the
process environment of the service that mounts it.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping

MAX_SECRET_BYTES = 64 * 1024


def secret(name: str, environ: Mapping[str, str] | None = None) -> str:
    """The credential's value, or an empty string when it is not provided."""
    environ = os.environ if environ is None else environ
    if not name:
        return ""
    path = environ.get(name + "_FILE", "")
    if path:
        if not os.path.isabs(path):
            raise ValueError(f"{name}_FILE must be an absolute path")
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SECRET_BYTES:
            raise ValueError(f"{name}_FILE must be a small regular file")
        with open(path, encoding="utf-8") as stream:
            return stream.read().rstrip("\r\n")
    return environ.get(name, "")
