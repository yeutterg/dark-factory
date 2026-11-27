"""One retrying JSON POST for the decision adapters."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from dark_factory.contracts import ContractError

RETRYABLE = {429, 502, 503, 529}


def post_json(
    url: str,
    body: dict[str, Any],
    key: str,
    *,
    timeout: float = 30.0,
    retries: int = 1,
    label: str = "request",
) -> dict[str, Any]:
    """POST JSON with a bearer key; retry transient failures with doubling backoff."""
    data = json.dumps(body).encode()
    error = ""
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(0.5 * 2 ** (attempt - 1))
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            error = f"HTTP {exc.code}"
            if exc.code not in RETRYABLE:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = str(exc)
    raise ContractError(f"{label} failed: {error}")
