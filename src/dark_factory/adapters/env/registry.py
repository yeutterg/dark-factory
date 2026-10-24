"""Execution backends, independent of workflow, harness and model selection."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from dark_factory.adapters.env.base import Environment
from dark_factory.adapters.env.native import NativeEnvironment


def docker_command(recipe):
    return [
        recipe.get("binary", "docker"),
        "--host",
        recipe.get("daemon_host", "unix:///var/run/docker.sock"),
    ]


def project_network(recipe, project):
    import subprocess

    network = recipe.get("network")
    if not network:
        raise RuntimeError(
            "network-enabled OCI tasks require a project-owned internal gateway network"
        )
    result = subprocess.run(
        [*docker_command(recipe), "network", "inspect", network],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError("approved sandbox network is unavailable")
    data = json.loads(result.stdout)[0]
    if (
        data.get("Internal") is not True
        or (data.get("Labels") or {}).get("com.dark-factory.project") != project
    ):
        raise RuntimeError("sandbox network must be internal and owned by this project")
    options = data.get("Options") or {}
    if (
        data.get("Driver") != "bridge"
        or options.get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
        or data.get("EnableIPv6")
        and options.get("com.docker.network.bridge.gateway_mode_ipv6") != "isolated"
    ):
        raise RuntimeError(
            "sandbox network must isolate the host gateway as well as external routing"
        )
    return network


class OciEnvironment(NativeEnvironment):
    name = "oci"

    def __init__(self, root, recipe, route=None):
        if os.getuid() == 0:
            raise RuntimeError("OCI execution workers must use a non-root host account")
        super().__init__(root)
        self.recipe, self.route = recipe, route or {}
        self.containers = set()

    @staticmethod
    def _container_name(workspace, runtime):
        return (
            "df-"
            + hashlib.sha256(
                (str(workspace.resolve()) + str(runtime.resolve())).encode()
            ).hexdigest()[:32]
        )

    def reconstruct(self, prepared, runtime, **policy):
        """Recover the deterministic owned container name without Docker calls."""

        self.containers.add(self._container_name(prepared.workspace, runtime))

    def prefix(
        self,
        prepared,
        runtime,
        *,
        network,
        writable,
        protected,
        immutable_runtime_files=(),
        **kwargs,
    ):
        workspace = prepared.workspace.resolve()
        runtime = runtime.resolve()
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(c in str(p) for p in (workspace, runtime) for c in (",", "\n")):
            raise ValueError("OCI bind paths cannot contain mount delimiters")
        recipe = self.recipe
        owner = hashlib.sha256(str(self.root).encode()).hexdigest()[:24]
        name = self._container_name(workspace, runtime)
        self.containers.add(name)
        argv = [
            *docker_command(recipe),
            "run",
            "--rm",
            # Harnesses send the brief on stdin; without -i the container sees none.
            "--interactive",
            "--pull=never",
            "--name",
            name,
            "--label",
            "com.dark-factory.owner=" + owner,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cpus",
            str(recipe.get("cpu", 1)),
            "--memory",
            str(recipe.get("memory_mb", 512)) + "m",
            "--memory-swap",
            str(recipe.get("memory_mb", 512)) + "m",
            "--pids-limit",
            str(recipe.get("pids", 128)),
            "--network",
            "none",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--workdir",
            str(workspace),
        ]
        if network and self.route.get("evidence_kind") == "live":
            argv[argv.index("--network") + 1] = project_network(
                recipe, self.route.get("project_id")
            )

        def mount(path, writable=False):
            if "," in str(path) or "\n" in str(path):
                raise ValueError("OCI mount path contains a delimiter")
            mode = "" if writable else ",readonly"
            argv.extend(["--mount", f"type=bind,source={path},target={path}{mode}"])

        mount(workspace, "." in writable)
        mount(runtime, True)
        for name in writable:
            if name == ".":
                continue
            path = (workspace / name).resolve()
            if not path.is_relative_to(workspace):
                raise ValueError("write path escapes workspace")
            if path.exists():
                mount(path, True)
            else:
                raise ValueError("OCI writable roots must exist in approved source")
        for name in dict.fromkeys(
            protected + [".git", ".pi", ".claude", ".mcp.json", ".agents", ".github"]
        ):
            path = (workspace / name).resolve()
            if not path.is_relative_to(workspace):
                raise ValueError("protected path escapes workspace")
            if path.exists():
                mount(path)
        for name in immutable_runtime_files:
            path = runtime / name
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("immutable path escapes runtime")
            mount(path)
        # Only execution variables and the selected route credential enter the container.
        for key in [
            "HOME",
            "TMPDIR",
            "LANG",
            "PYTHONDONTWRITEBYTECODE",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
            "PI_OFFLINE",
            "PI_TELEMETRY",
            self.route.get("auth_env", ""),
        ]:
            if key:
                argv.extend(["--env", key])
        argv.append(recipe["image"])
        return argv

    def stop(self, prepared):
        import subprocess

        owner = hashlib.sha256(str(self.root).encode()).hexdigest()[:24]
        for name in self.containers:
            result = subprocess.run(
                [
                    *docker_command(self.recipe),
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "com.dark-factory.owner"}}',
                    name,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode:
                self._verify_absent(name)
                continue  # Confirmed --rm already removed the completed container.
            if result.stdout.strip() != owner:
                raise RuntimeError("refusing removal of an unowned container")
            result = subprocess.run(
                [*docker_command(self.recipe), "rm", "--force", name],
                capture_output=True,
                timeout=10,
            )
            if result.returncode:
                raise RuntimeError("owned container teardown failed")
            self._verify_absent(name)

    def _verify_absent(self, name):
        import subprocess

        result = subprocess.run(
            [
                *docker_command(self.recipe),
                "ps",
                "--all",
                "--filter",
                "name=^/" + name + "$",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode or name in result.stdout.splitlines():
            raise RuntimeError(
                "cannot verify owned container removal; resources retained"
            )

    def release(self, prepared):
        # Release owns the full teardown contract. Calling stop beforehand is
        # harmless and keeps normal execution and crash-recovery paths symmetric.
        self.stop(prepared)
        super().release(prepared)


class CommandEnvironment(NativeEnvironment):
    """Trusted VM/remote sandbox driver; argv contract, never a model-provided shell."""

    name = "command"

    def __init__(self, root, recipe, route=None):
        super().__init__(root)
        self.recipe = recipe
        self.descriptors = {}

    def prefix(self, prepared, runtime, **policy):
        from dark_factory.artifacts import digest

        for path, expected in self.recipe.get("driver_files", {}).items():
            if digest(Path(path)) != expected:
                raise RuntimeError("external sandbox driver changed before execution")
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = {
            "version": 1,
            "workspace": str(prepared.workspace.resolve()),
            "runtime": str(runtime.resolve()),
            "network": policy["network"],
            "writable": policy["writable"],
            "protected": policy["protected"],
            "immutable_runtime_files": list(policy.get("immutable_runtime_files", ())),
            "self_sandbox_binaries": self.recipe.get("self_sandbox_binaries", []),
            "limits": {
                k: self.recipe.get(k, v)
                for k, v in (("cpu", 1), ("memory_mb", 512), ("pids", 128))
            },
        }
        self.descriptors[str(runtime.resolve())] = descriptor
        return [
            *self.recipe["argv"],
            "--factory-policy",
            json.dumps(descriptor, sort_keys=True),
            "--",
        ]

    def _operation(self, operation):
        import subprocess

        for descriptor in self.descriptors.values():
            result = subprocess.run(
                [
                    *self.recipe["argv"],
                    "--factory-policy",
                    json.dumps(descriptor, sort_keys=True),
                    "--operation",
                    operation,
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode:
                raise RuntimeError(
                    "external sandbox " + operation + " failed; resources retained"
                )

    def stop(self, prepared):
        self._operation("stop")

    def reconstruct(self, prepared, runtime, **policy):
        self.prefix(prepared, runtime, **policy)

    def release(self, prepared):
        self._operation("release")
        super().release(prepared)


def environment_for(root, recipe, route=None) -> Environment:
    kind = recipe["kind"]
    if kind in {"native", "macos-sandbox"}:
        return NativeEnvironment(root)
    if kind == "oci":
        return OciEnvironment(root, recipe, route)
    if kind == "command":
        return CommandEnvironment(root, recipe, route)
    raise ValueError("unsupported environment; no fallback")
