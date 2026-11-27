import io
import json

from dark_factory import log
from dark_factory.store import Store


def test_audit_records_become_json_lines_with_the_ids_lifted(tmp_path):
    stream = io.StringIO()
    log.configure(stream)
    store = Store(tmp_path)
    try:
        store.audit(
            "worker-1", "attempt.finish", {"attempt_id": "att_1", "status": "failed"}
        )
        store.audit("controller", "gate.error", {"error": "boom" * 200, "root": "wi_9"})
    finally:
        store.close()
        import logging

        logging.getLogger("dark_factory").handlers.clear()
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["event"] == "attempt.finish" and lines[0]["attempt_id"] == "att_1"
    assert (
        lines[0]["actor"] == "worker-1"
        and lines[0]["level"] == "info"
        and "ts" in lines[0]
    )
    assert lines[1]["level"] == "warning" and lines[1]["root"] == "wi_9"
    assert len(lines[1]["error"]) == 300  # bounded


def test_nothing_is_logged_until_logging_is_configured(tmp_path, capsys):
    store = Store(tmp_path)
    try:
        store.audit("a", "x", {})
    finally:
        store.close()
    assert capsys.readouterr().err == ""
