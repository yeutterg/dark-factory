"""Classify interrupted attempts and decide, in code, whether they may continue.

An interruption is an attempt that stopped for a reason other than its result:
a lost worker, an engine or provider fault, a step time limit or unusable output.
Correctness failures (failed checks, blocking findings) and policy failures
(changed approval, scope or budget violations, integrity) are not interruptions.

Rules give a first answer. An optional decision agent may confirm or overturn it
when it is confident. Hard limits are always code: the approved resume count, the
root deadline and the approved spending ceiling. A model can never lift them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Causes a person rarely needs to look at: the step should just continue.
TRANSIENT = frozenset({"worker_lost", "engine", "provider", "invalid_output"})

CATEGORY_TEXT = {
    "worker_lost": "the worker stopped responding",
    "engine": "the dispatch engine failed",
    "provider": "the model provider returned a temporary error",
    "invalid_output": "the agent's answer was not in the expected format",
    "time_limit": "the step ran out of time",
    "credentials": "the route's credential was unavailable",
    "archive": "the attempt's work could not be archived",
    "unknown": "the step stopped unexpectedly",
}

# Failures that enforce approved scope or evidence integrity; never resumed.
POLICY_MARKERS = (
    "approved inputs changed",
    "trusted command binding changed",
    "protected or out-of-scope",
    "candidate missing",
    "candidate input missing",
    "modified candidate source",
    "spending exceeded",
    "invalid durable archive",
    "live execution is not qualified",
)

_PROVIDER = re.compile(
    r"\b(429|5\d\d)\b|rate.?limit|overloaded|temporarily unavailable|"
    r"connection (reset|refused|closed)|econnreset|etimedout|network|"
    r"timed out (connecting|waiting for (the )?(response|provider))|"
    r"service unavailable|bad gateway|gateway timeout|try again",
    re.IGNORECASE,
)


def categorize(error: str, timed_out: bool) -> str:
    """The cause of an interruption, from facts the controller recorded."""

    text = error or ""
    if "worker interrupted" in text:
        return "worker_lost"
    if text.startswith("durable dispatch failed") or "workflow runtime failed" in text:
        return "engine"
    if text.startswith("invalid role result"):
        return "invalid_output"
    if text.startswith("archive failed"):
        return "archive"
    if "credential unavailable" in text:
        return "credentials"
    if _PROVIDER.search(text):
        return "provider"
    if timed_out:
        return "time_limit"
    return "unknown"


def is_interruption(error: str, timed_out: bool, failure_kind: str | None) -> bool:
    """Whether a failed attempt was interrupted rather than judged.

    The caller has already excluded cancellation, correctness failures and policy
    failures. Durable inputs changing is policy, never an interruption.
    """

    text = error or ""
    if text == "durable dispatch failed: inputs_changed" or any(
        marker in text for marker in POLICY_MARKERS
    ):
        return False
    return (
        timed_out
        or failure_kind in {"execution", "harness"}
        or text.startswith(("invalid role result", "archive failed"))
        or "worker interrupted" in text
        or text.startswith("durable dispatch failed")
    )


@dataclass
class Verdict:
    """What happens next, and why."""

    action: str  # "resume" (automatically) or "ask" (a person decides)
    recommendation: str  # "resume" or "stop": what a person is advised to do
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)  # Nobody may resume past these.


def decide(
    category: str,
    automatic_resumes: int,
    max_resumes: int,
    blockers: list[str],
    decision: dict | None = None,
    min_confidence: float = 0.8,
) -> Verdict:
    """Combine rules, an optional decision agent and code-enforced limits."""

    suggestion = "resume" if category in TRANSIENT else "ask"
    reasons = [f"rules: {CATEGORY_TEXT.get(category, category)}"]
    recommendation = "resume"
    if decision:
        choice = decision.get("decision")
        confidence = decision.get("confidence")
        if (
            choice in {"resume", "ask", "stop"}
            and isinstance(confidence, int | float)
            and confidence >= min_confidence
        ):
            suggestion = "resume" if choice == "resume" else "ask"
            recommendation = "stop" if choice == "stop" else "resume"
            reasons.append(
                f"{decision.get('agent', 'decider')}: {choice} ({confidence:.2f})"
            )
        else:
            reasons.append(
                f"{decision.get('agent', 'decider')}: not confident; rules kept"
            )
    if blockers:
        return Verdict("ask", "stop", reasons, list(blockers))
    if suggestion == "resume" and automatic_resumes >= max_resumes:
        reasons.append(f"automatic resumes used ({automatic_resumes} of {max_resumes})")
        return Verdict("ask", recommendation, reasons)
    return Verdict(suggestion, recommendation, reasons)
