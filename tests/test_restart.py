from __future__ import annotations

from pathlib import Path

from dark_factory.config import parse_toml_bytes
from dark_factory.controller import Controller
from dark_factory.store import Store
from dark_factory.worker import Worker


def test_restart_resumes_from_sqlite(tmp_path: Path) -> None:
    text = Path("examples/factory.toml").read_text(encoding="utf-8")
    text = text.replace('state_dir = "./state"', f'state_dir = "{tmp_path}"')
    cfg = parse_toml_bytes(text.encode(), Path("examples/factory.toml").resolve())
    store = Store(cfg.state_dir)
    ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
    ids = ctrl.intake_issues(
        [{"repo": "yeutterg/dark-factory", "number": 9, "title": "Restart", "body": "", "is_root": True}]
    )
    ctrl.start_planning(ids[0])
    store.close()

    store2 = Store(cfg.state_dir)
    ctrl2 = Controller(cfg, store2, Path("src/dark_factory/agents/prompts"))
    worker = Worker(cfg, store2, ctrl2, "restart")
    worker.run_loop(max_jobs=2)
    item = store2.one("SELECT status FROM work_items WHERE id=?", (ids[0],))
    assert item["status"] == "awaiting_approval"
