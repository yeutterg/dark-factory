"""Explicit harness catalogue; adding a CLI does not change worker execution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from dark_factory.adapters.harness.claude_code import ClaudeCodeHarness
from dark_factory.adapters.harness.codex import CodexHarness
from dark_factory.adapters.harness.fake import FakeHarness
from dark_factory.adapters.harness.pi import PiHarness, resolve_credential
from dark_factory.runner import CommandRunner


def explicit_credential(route):
    return os.environ.get(route.get("auth_env", ""), "")


@dataclass(frozen=True)
class HarnessAdapter:
    name: str
    factory: type
    binary_key: str = ""
    binary_default: str = ""
    roles: frozenset[str] = frozenset({"planner", "critic", "coder", "reviewer"})
    credential_resolver: Callable[[dict], str] = explicit_credential
    supports_effort: bool = False
    session_paths: tuple[str, ...] = ()

    def binary(self, cfg):
        return cfg.execution.get(self.binary_key, self.binary_default)

    def available(self, cfg):
        return not self.binary_key or self.factory.available(self.binary(cfg))

    def open(self, cfg, route):
        if not self.binary_key:
            return self.factory()
        binary = self.binary(cfg)
        actual = CommandRunner(cfg.source_path.parent).run_plain(
            [binary, "--version"], 10
        )
        if actual[0] or actual[1].strip() != route["harness_version"]:
            raise RuntimeError(f"{self.name} version differs from pinned route")
        return self.factory(binary)


ADAPTERS = {
    adapter.name: adapter
    for adapter in (
        HarnessAdapter("fake", FakeHarness),
        HarnessAdapter(
            "pi",
            PiHarness,
            "pi_binary",
            "pi",
            credential_resolver=resolve_credential,
            session_paths=("agent/sessions",),
        ),
        HarnessAdapter(
            "codex",
            CodexHarness,
            "codex_binary",
            "codex",
            supports_effort=True,
            session_paths=("codex-agent/sessions",),
        ),
        HarnessAdapter(
            "claude-code",
            ClaudeCodeHarness,
            "claude_binary",
            "claude",
            frozenset({"planner", "critic", "reviewer"}),
            supports_effort=True,
        ),
    )
}


def adapter_for(name):
    try:
        return ADAPTERS[name]
    except KeyError:
        raise RuntimeError("unsupported harness; no fallback permitted") from None


def capabilities(cfg):
    return [
        "native",
        *(name for name, adapter in ADAPTERS.items() if adapter.available(cfg)),
    ]


def credential_for(route):
    return adapter_for(route.get("harness", "pi")).credential_resolver(route)
