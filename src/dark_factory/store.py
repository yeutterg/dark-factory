"""SQLite store for execution state."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_items (
  id TEXT PRIMARY KEY,
  external_id TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  parent_id TEXT,
  repo TEXT,
  status TEXT NOT NULL,
  is_root INTEGER NOT NULL,
  iteration TEXT,
  plan_version INTEGER NOT NULL DEFAULT 0,
  approved_digest TEXT,
  approved_by TEXT,
  approved_at TEXT,
  spending_ceiling_cents INTEGER NOT NULL DEFAULT 0,
  reserved_cents INTEGER NOT NULL DEFAULT 0,
  spent_cents INTEGER NOT NULL DEFAULT 0,
  candidate_commits TEXT NOT NULL DEFAULT '{}',
  presentation_stage TEXT NOT NULL DEFAULT 'Plan',
  plan_json TEXT,
  critique_json TEXT,
  packet_json TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  lease_until TEXT,
  worker_id TEXT,
  reserved_cents INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (work_item_id) REFERENCES work_items(id)
);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  worker_id TEXT,
  status TEXT NOT NULL,
  workspace TEXT,
  route_id TEXT,
  started_at TEXT,
  ended_at TEXT,
  cost_cents INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  config_hash TEXT,
  prompt_hash TEXT,
  archive_path TEXT,
  FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT,
  digest TEXT,
  payload_json TEXT NOT NULL,
  inputs_json TEXT NOT NULL,
  FOREIGN KEY (attempt_id) REFERENCES attempts(id)
);
CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY,
  capabilities TEXT NOT NULL,
  last_heartbeat TEXT,
  busy INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS costs (
  id TEXT PRIMARY KEY,
  root_id TEXT NOT NULL,
  attempt_id TEXT,
  source TEXT NOT NULL,
  amount_cents INTEGER,
  currency TEXT NOT NULL DEFAULT 'USD',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id TEXT PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ops (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  actor TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config_revisions (
  digest TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  valid INTEGER NOT NULL,
  loaded_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


_OWNERS: dict[str, tuple[int, Any, int]] = {}
_OWNER_LOCK = threading.Lock()


class Store:
    def __init__(self, state_dir: Path) -> None:
        self._lock = threading.RLock()
        self._transaction_depth = 0
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._owner_key = str(self.state_dir.resolve())
        with _OWNER_LOCK:
            existing = _OWNERS.get(self._owner_key)
            if existing and existing[0] == os.getpid():
                _OWNERS[self._owner_key] = (existing[0], existing[1], existing[2] + 1)
            else:
                lock = (self.state_dir / "controller.lock").open("a+")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    lock.close()
                    raise RuntimeError("state directory already has a controller owner")
                _OWNERS[self._owner_key] = (os.getpid(), lock, 1)
        (self.state_dir / "artifacts").mkdir(exist_ok=True)
        (self.state_dir / "archives").mkdir(exist_ok=True)
        self.db_path = self.state_dir / "factory.sqlite"
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        existing_tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "meta" in existing_tables:
            version = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if version and version[0] != "2":
                self.close()
                raise RuntimeError(
                    "incompatible state schema; restore with its pinned controller version"
                )
        if "work_items" in existing_tables:
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(work_items)")}
            if (
                "snapshot_json" not in columns
                and self.conn.execute("SELECT 1 FROM work_items LIMIT 1").fetchone()
            ):
                self.close()
                raise RuntimeError(
                    "legacy scaffold state has no pinned inputs; preserve it and use a new bootstrap state directory"
                )
        self.conn.executescript(SCHEMA)
        for table, columns in {
            "work_items": {
                "snapshot_json": "TEXT",
                "created_at": "TEXT",
                "accepted_at": "TEXT",
            },
            "attempts": {
                "inputs_json": "TEXT",
                "result_json": "TEXT",
                "heartbeat_at": "TEXT",
                "deadline": "TEXT",
                "pid": "INTEGER",
                "pid_identity": "TEXT",
                "cleanup_status": "TEXT",
            },
        }.items():
            existing = {
                row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            for column, kind in columns.items():
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS proposals (
            root_id TEXT NOT NULL, version INTEGER NOT NULL, payload_json TEXT NOT NULL,
            approved_digest TEXT, actor TEXT, approved_at TEXT,
            PRIMARY KEY(root_id, version));
        CREATE TABLE IF NOT EXISTS manual_gates (
            root_id TEXT NOT NULL, candidate TEXT NOT NULL, name TEXT NOT NULL,
            actor TEXT NOT NULL, at TEXT NOT NULL, passed INTEGER NOT NULL,
            evidence TEXT NOT NULL, PRIMARY KEY(root_id, candidate, name));
        """)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('schema_version','2')"
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
        with _OWNER_LOCK:
            owner = _OWNERS.get(self._owner_key)
            if owner and owner[2] == 1:
                owner[1].close()
                del _OWNERS[self._owner_key]
            elif owner:
                _OWNERS[self._owner_key] = (owner[0], owner[1], owner[2] - 1)

    @contextmanager
    def transaction(self):
        """Serialize a complete policy decision, including across SQLite connections."""
        with self._lock:
            outer = self._transaction_depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
                if outer:
                    self.conn.commit()
            except BaseException:
                if outer:
                    self.conn.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            if not self._transaction_depth:
                self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params))

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value)
        row = self.one("SELECT value FROM meta WHERE key='restore_origins'")
        if row and path.is_absolute():
            for original_value in json.loads(row["value"]):
                original = Path(original_value)
                if path.is_relative_to(original):
                    return self.state_dir.resolve() / path.relative_to(original)
        return path

    def audit(self, actor: str, action: str, payload: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO audit(id, at, actor, action, payload_json) VALUES (?,?,?,?,?)",
            (new_id("aud"), now_iso(), actor, action, json.dumps(payload)),
        )

    def set_op(self, key: str, value: str, actor: str) -> None:
        self.execute(
            "INSERT INTO ops(key, value, actor, at) VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, actor=excluded.actor, at=excluded.at",
            (key, value, actor, now_iso()),
        )
        self.audit(actor, f"ops.{key}", {"value": value})

    def get_op(self, key: str, default: str = "off") -> str:
        row = self.one("SELECT value FROM ops WHERE key=?", (key,))
        return row["value"] if row else default

    def save_config_revision(self, digest: str, body: str, valid: bool) -> None:
        self.execute(
            "INSERT OR REPLACE INTO config_revisions(digest, body, valid, loaded_at) VALUES (?,?,?,?)",
            (digest, body, 1 if valid else 0, now_iso()),
        )

    def last_valid_config(self) -> Optional[str]:
        row = self.one(
            "SELECT body FROM config_revisions WHERE valid=1 ORDER BY loaded_at DESC LIMIT 1"
        )
        return row["body"] if row else None
