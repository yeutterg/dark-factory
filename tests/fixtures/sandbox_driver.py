"""Simulation-only sandbox driver contract fixture. NOT a VM or security boundary."""

import argparse
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--factory-policy", required=True)
parser.add_argument("--operation")
args, command = parser.parse_known_args()
policy = json.loads(args.factory_policy)
runtime = Path(policy["runtime"])
runtime.mkdir(parents=True, exist_ok=True)
with (runtime / "driver-events.jsonl").open("a") as output:
    output.write(
        json.dumps({"operation": args.operation or "execute", "policy": policy}) + "\n"
    )
if args.operation:
    raise SystemExit(0)
if command and command[0] == "--":
    command.pop(0)
raise SystemExit(subprocess.run(command, check=False).returncode)
