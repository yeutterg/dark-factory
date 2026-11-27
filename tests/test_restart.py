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
        [
            {
                "repo": "yeutterg/dark-factory",
                "number": 9,
                "title": "Restart",
                "body": "",
                "is_root": True,
            }
        ]
    )
    ctrl.start_planning(ids[0])
    store.close()

    store2 = Store(cfg.state_dir)
    ctrl2 = Controller(cfg, store2, Path("src/dark_factory/agents/prompts"))
    worker = Worker(cfg, store2, ctrl2, "restart")
    worker.run_loop(max_jobs=2)
    item = store2.one("SELECT status FROM work_items WHERE id=?", (ids[0],))
    assert item["status"] == "awaiting_approval"


def test_state_from_a_newer_controller_is_refused_and_older_is_upgraded(tmp_path):
    import sqlite3

    import pytest

    from dark_factory.store import SCHEMA_VERSION, Store

    store = Store(tmp_path)
    assert store.one("SELECT value FROM meta WHERE key='schema_version'")[0] == str(
        SCHEMA_VERSION
    )
    store.close()
    db = sqlite3.connect(tmp_path / "factory.sqlite")
    db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    db.commit()
    db.close()
    upgraded = Store(tmp_path)  # an older state is migrated in place
    assert upgraded.one("SELECT value FROM meta WHERE key='schema_version'")[0] == str(
        SCHEMA_VERSION
    )
    upgraded.close()
    db = sqlite3.connect(tmp_path / "factory.sqlite")
    db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match="newer controller"):
        Store(tmp_path)


def test_database_is_durable_by_configuration_and_checked_on_open(tmp_path):
    import pytest

    from dark_factory.store import Store

    store = Store(tmp_path)
    assert store.one("PRAGMA journal_mode")[0] == "wal"
    assert store.one("PRAGMA synchronous")[0] == 2  # FULL
    assert store.one("PRAGMA busy_timeout")[0] == 30000
    store.close()
    db = tmp_path / "factory.sqlite"
    data = bytearray(db.read_bytes())
    for offset in range(4096 * 2, min(len(data), 4096 * 6)):
        data[offset] = 0xFF
    db.write_bytes(bytes(data))
    (tmp_path / "factory.sqlite-wal").unlink(missing_ok=True)
    with pytest.raises(RuntimeError, match="integrity check"):
        Store(tmp_path)


def test_timestamps_sort_within_one_second():
    from dark_factory.store import now_iso

    first, second = now_iso(), now_iso()
    assert first <= second and "." in first
