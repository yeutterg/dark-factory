"""Owned Git workspaces and qualified native macOS process isolation."""

from __future__ import annotations

import json
import platform
import shutil
from pathlib import Path

from dark_factory.adapters.env.base import PreparedEnv
from dark_factory.adapters.git.local import clone
from dark_factory.runner import CommandRunner


class NativeEnvironment:
    name = "native"

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(self, work_item_id: str, repo=None) -> PreparedEnv:
        if Path(work_item_id).name != work_item_id or work_item_id in {".", ".."}:
            raise ValueError("invalid owned workspace identifier")
        workspace = self.root / work_item_id
        if workspace.exists():
            raise FileExistsError(workspace)
        if isinstance(repo, dict):
            clone(Path(repo["path"]), workspace, repo["commit"])
        else:
            workspace.mkdir()
            (workspace / "REPO").write_text(repo or "local")
        return PreparedEnv(workspace, self.name, {"repo": repo})

    def prefix(
        self,
        prepared: PreparedEnv,
        runtime: Path,
        *,
        network: bool,
        writable: list[str],
        protected: list[str],
        private_paths: list[Path] | None = None,
        readable_files: list[Path] | None = None,
    ) -> list[str]:
        if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").exists():
            raise RuntimeError("macos-sandbox requires sandbox-exec on macOS")
        workspace = prepared.workspace.resolve()
        runtime = runtime.resolve()
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Native isolation is weaker than a VM. Deny user/private data, all writes,
        # networking by default and cross-process control; selectively admit owned paths.
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-write* network*)",
            "(deny signal)",
            "(allow signal (target same-sandbox))",
            "(deny appleevent-send)",
            '(deny file-read-data (subpath "/Users") (subpath "/private/tmp") (subpath "/private/var/folders") (subpath "/private/var/root") (subpath "/Volumes"))',
            *[
                f"(deny file-read-data (subpath {json.dumps(str(path.resolve()))}))"
                for path in [self.root.parent, *(private_paths or [])]
            ],
            f"(allow file-read-data (subpath {json.dumps(str(workspace))}))",
            f"(allow file-read-data file-write* (subpath {json.dumps(str(runtime))}))",
            '(allow file-write* (literal "/dev/null"))',
        ]
        for path in readable_files or []:
            if not path.is_file():
                raise ValueError("harness read grant must name an existing file")
            lines.append(
                f"(allow file-read-data (literal {json.dumps(str(path.resolve()))}))"
            )
        for name in writable:
            path = (workspace / name).resolve()
            if not path.is_relative_to(workspace):
                raise ValueError("write path escapes workspace")
            lines.append(f"(allow file-write* (subpath {json.dumps(str(path))}))")
        for name in protected + [
            ".git",
            ".pi",
            ".claude",
            ".mcp.json",
            ".agents",
            ".github",
        ]:
            path = (workspace / name).resolve()
            if not path.is_relative_to(workspace):
                raise ValueError("protected path escapes workspace")
            lines.append(f"(deny file-write* (subpath {json.dumps(str(path))}))")
        if network:
            lines.append("(allow network-outbound (remote tcp))")
        policy = runtime / "sandbox.sb"
        policy.write_text("\n".join(lines))
        return ["/usr/bin/sandbox-exec", "-f", str(policy)]

    def run(
        self, argv: list[str], cwd: Path, timeout_seconds: int
    ) -> tuple[int, str, str]:
        cwd = cwd.resolve()
        if cwd.parent != self.root:
            raise ValueError("execution must use an owned workspace")
        runtime = self.root.parent / "runtime" / (cwd.name + "-command")
        prepared = PreparedEnv(cwd, self.name, {})
        prefix = self.prefix(
            prepared, runtime, network=False, writable=[], protected=[]
        )
        code, out, err = CommandRunner(cwd)._run(
            prefix + argv, b"", timeout_seconds, cwd, process_environment(runtime)
        )
        return code, out.decode(errors="replace"), err.decode(errors="replace")

    def stop(self, prepared: PreparedEnv) -> None:
        # Every execution is synchronously supervised by CommandRunner, which kills
        # its entire process group before returning or raising.
        pass

    def release(self, prepared: PreparedEnv) -> None:
        if (
            prepared.workspace.parent.resolve() != self.root
            or prepared.workspace.is_symlink()
        ):
            raise ValueError("refusing cleanup outside owned workspace root")
        if prepared.workspace.exists():
            shutil.rmtree(prepared.workspace)


def process_environment(
    runtime: Path, credentials: dict[str, str] | None = None
) -> dict[str, str]:
    runtime = runtime.resolve()
    (runtime / "home").mkdir(parents=True, exist_ok=True)
    (runtime / "tmp").mkdir(exist_ok=True)
    return {
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(runtime / "home"),
        "TMPDIR": str(runtime / "tmp"),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        **(credentials or {}),
    }
