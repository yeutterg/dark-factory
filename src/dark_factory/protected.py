"""Paths no agent may change, in any environment, whatever a profile says.

One list is used by every sandbox adapter and by candidate capture, so an
environment cannot forget an entry that capture enforces, or the reverse.
"""

from __future__ import annotations

from collections.abc import Iterable

ALWAYS_PROTECTED = (
    ".git",
    ".github",
    ".gitmodules",
    ".pi",
    ".claude",
    ".mcp.json",
    ".agents",
)


def protected_paths(configured: Iterable[str] = ()) -> list[str]:
    """The profile's protected paths plus the paths protected unconditionally."""
    return list(dict.fromkeys([*configured, *ALWAYS_PROTECTED]))
