"""A worker process for the kill-and-restart soak test. Not a test itself."""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_loop import cfg_for  # noqa: E402

from dark_factory.adapters.harness.fake import FakeHarness  # noqa: E402
from dark_factory.config import FactoryConfig  # noqa: E402
from dark_factory.controller import Controller  # noqa: E402
from dark_factory.store import Store  # noqa: E402
from dark_factory.worker import Worker  # noqa: E402


class Slow(FakeHarness):
    def start(self, prompt, workspace, extra=None):
        marker = os.environ.get("SOAK_MARKER")
        if marker:
            Path(marker).touch()  # tells the test an attempt is now running
        time.sleep(0.8)  # long enough for a kill to land mid-attempt
        yield from super().start(prompt, workspace, extra)


FactoryConfig.lease_seconds = lambda self, worker_id: 2


def main(state):
    cfg = cfg_for(Path(state))
    cfg.windows = []
    # A killed attempt is charged its whole reservation; leave room for several.
    cfg.raw["budgets"] = {**cfg.raw["budgets"], "planning_cents": 1000}
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    Worker._harness = lambda self, route: Slow()
    while True:
        ctrl.recover("soak")
        ctrl.settle_interruptions("soak")
        Worker(cfg, store, ctrl, "soak").run_loop()
        time.sleep(0.2)


if __name__ == "__main__":
    main(sys.argv[1])
