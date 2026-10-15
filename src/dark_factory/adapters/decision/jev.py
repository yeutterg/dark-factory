"""TypeSafe Jev as a decision agent: calibrated typed answers, no generated text.

    POST https://api.typesafe.ai/v1/systemone   Authorization: Bearer <key>

The key is read from the environment variable named by ``--api-key-env``
(default ``TYPESAFE_API_KEY``). Without it the command fails and the controller
keeps its rules.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from dark_factory.contracts import ContractError, validate_request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
MAX_CHOICE_OPTIONS = 255
RETRYABLE = {429, 502, 503, 529}
MAX_STATE_CHARS = 60_000


def post(body, key, timeout=20.0, retries=3):
    data = json.dumps(body).encode()
    error = ""
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(0.5 * 2 ** (attempt - 1))
        request = urllib.request.Request(
            ENDPOINT,
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
    raise ContractError(f"jev request failed: {error}")


def handle(request, key, model=DEFAULT_MODEL, send=post):
    validate_request(request)
    if request["operation"] != "decide":
        raise ContractError(f"unsupported decision operation {request['operation']!r}")
    payload = request["input"]
    questions = payload.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise ContractError("questions required")
    asked = {}
    for name, question in questions.items():
        options = question.get("options") if isinstance(question, dict) else None
        if (
            question.get("type") != "choice"
            or not isinstance(options, dict)
            or not 2 <= len(options) <= MAX_CHOICE_OPTIONS
        ):
            raise ContractError(f"question {name!r} must be a choice of 2-255 options")
        asked[name] = {
            "type": "choice",
            "instructions": str(question.get("instructions", "")),
            "criteria": {str(k): str(v) for k, v in options.items()},
        }
    state = json.dumps(payload.get("state", {}), sort_keys=True)
    if len(state) > MAX_STATE_CHARS:
        raise ContractError("decision state exceeds the Jev input budget")
    reply = send({"model": model, "state": json.loads(state), "questions": asked}, key)
    answers = {}
    for name, question in asked.items():
        answer = (reply.get("answers") or {}).get(name) or {}
        choice = answer.get("choice")
        confidence = answer.get("confidence")
        if choice not in question["criteria"] or not isinstance(
            confidence, int | float
        ):
            raise ContractError(f"jev returned no usable answer for {name!r}")
        answers[name] = {
            "choice": choice,
            "confidence": float(confidence),
            "probabilities": answer.get("probabilities", {}),
        }
    return {
        "contract_version": 1,
        "request_id": request["request_id"],
        "ok": True,
        "operation": "decide",
        "evidence": {
            "agent": "jev",
            "model": reply.get("model", model),
            "answers": answers,
            "usage": reply.get("usage", {}),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dark_factory.adapters.decision.jev")
    parser.add_argument("--api-key-env", default="TYPESAFE_API_KEY")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    try:
        key = os.environ.get(args.api_key_env, "")
        if not key:
            raise ContractError(f"{args.api_key_env} is not set")
        print(json.dumps(handle(json.load(sys.stdin), key, args.model)))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
