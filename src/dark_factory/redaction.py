"""One redaction pass for text that leaves a harness: stderr, errors, output, events.

Exact credential values are replaced first; then common credential shapes, so a
token the worker was never given (one the agent found) is still masked.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MIN_SECRET_LENGTH = 8
SHAPES = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16}|tgp_v1_[A-Za-z0-9_-]{20,})"
    r"|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
MASK = "[REDACTED]"


def redact(text: str | None, secrets: Iterable[str] = ()) -> str:
    """The text with known credential values and credential-shaped strings masked."""
    if not text:
        return text or ""
    for value in sorted(
        {s for s in secrets if isinstance(s, str) and len(s) >= MIN_SECRET_LENGTH},
        key=len,
        reverse=True,
    ):
        text = text.replace(value, MASK)
    return SHAPES.sub(MASK, text)
