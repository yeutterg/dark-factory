"""Remote worker robustness: corrupt results, transport errors, repeated preflight."""

import json

import pytest

from dark_factory.execution.client import WorkerClient, write_json_atomic


def bare_client():
    return object.__new__(WorkerClient)


def assignment():
    return {
        "claim_digest": "d" * 64,
        "claim": {"attempt_id": "att_abcdef123456", "spec": {"budget_cents": 7}},
    }


def test_corrupt_executor_result_is_quarantined_and_settled_conservatively(tmp_path):
    (tmp_path / "result.json").write_text('{"attempt_id": "att_ab')  # torn write
    (tmp_path / "executor.log").write_text("Traceback: boom")
    result = bare_client()._load_result(tmp_path, assignment())
    assert (tmp_path / "result.corrupt").exists()
    receipt = result["receipt"]
    assert receipt["ok"] is False
    assert receipt["cost"] == 7  # unknown usage is charged conservatively
    assert "unreadable" in receipt["error"]
    assert "Traceback: boom" in receipt["error"]  # stderr tail is kept
    # The synthesized result is now what is on disk, so a retry reads it cleanly.
    again = bare_client()._load_result(tmp_path, assignment())
    assert again == result


def test_result_for_another_attempt_is_not_trusted(tmp_path):
    write_json_atomic(
        tmp_path / "result.json",
        {"attempt_id": "att_other", "claim_digest": "x", "receipt": {}},
    )
    result = bare_client()._load_result(tmp_path, assignment())
    assert result["attempt_id"] == "att_abcdef123456"
    assert (tmp_path / "result.corrupt").exists()


def test_atomic_write_leaves_no_temporary_file(tmp_path):
    write_json_atomic(tmp_path / "a.json", {"x": 1})
    assert json.loads((tmp_path / "a.json").read_text()) == {"x": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_worker_survives_transport_errors_with_capped_backoff():
    client = bare_client()
    outcomes: list = []
    sleeps, events = [], []

    def run_once():
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client.run_once = run_once

    class Stop(Exception):
        pass

    def sleep(seconds):
        sleeps.append(seconds)
        if not outcomes:
            raise Stop

    outcomes[:] = [OSError("down")] * 8 + [True]
    with pytest.raises(Stop):
        client.serve(sleep=sleep, log=events.append)
    assert sleeps[:8] == [1, 2, 4, 8, 16, 32, 60, 60]  # capped
    assert all("worker_error" in e for e in events)


def test_bounded_run_gives_up_after_repeated_failure():
    client = bare_client()

    def run_once():
        raise OSError("down")

    client.run_once = run_once
    with pytest.raises(OSError):
        client.serve(max_jobs=1, sleep=lambda s: None, log=lambda e: None)


def test_idle_polls_do_not_repeat_the_expensive_preflight(tmp_path):
    client = bare_client()
    client.state = tmp_path
    client._preflighted = None
    calls = []
    client._preflight = lambda info: calls.append(info)
    client.request = lambda op, payload: {"version": 1} if op == "preflight" else None
    (tmp_path / "worker.lock").touch()
    for _ in range(3):
        assert client.run_once() is False
    assert len(calls) == 1


def test_in_tree_relative_symlinks_travel_and_escaping_ones_are_refused(tmp_path):
    from dark_factory.execution.packages import pack, unpack

    tree = tmp_path / "tree"
    (tree / "pkg" / "bin").mkdir(parents=True)
    (tree / "pkg" / "cli.js").write_text("x")
    (tree / "pkg" / "bin" / "tool").symlink_to("../cli.js")  # stays inside
    package = pack({"source": tree})
    out = unpack(package, tmp_path / "out")
    assert (out / "source" / "pkg" / "bin" / "tool").resolve() == (
        out / "source" / "pkg" / "cli.js"
    ).resolve()

    (tree / "escape").symlink_to("../../outside")
    with pytest.raises(ValueError, match="escaping"):
        pack({"source": tree})
    (tree / "escape").unlink()
    (tree / "absolute").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="absolute"):
        pack({"source": tree})


def test_every_streaming_harness_stops_its_cli_when_the_consumer_stops():
    import threading
    import time

    from dark_factory.adapters.harness.streaming import StreamingCliHarness

    class Blocking(StreamingCliHarness):
        stopped = threading.Event()

        def run_events(self):
            def run(emit):
                while not self._cancel.is_set():
                    time.sleep(0.01)
                Blocking.stopped.set()

            yield from self._stream(run)

    harness = Blocking("x")
    stream = harness.run_events()
    next(stream)  # the consumer sees one event, then goes away
    stream.close()
    assert Blocking.stopped.wait(2), "the CLI kept running after its consumer stopped"


def test_heartbeat_only_cancels_on_explicit_refusal():
    client = bare_client()
    client.request = lambda op, payload: False
    assert client._heartbeat("att_x") is False  # the controller said no: cancel

    def down(op, payload):
        raise OSError("network down")

    client.request = down
    assert client._heartbeat("att_x") is None  # unknown: the lease decides, not us
    client.request = lambda op, payload: True
    assert client._heartbeat("att_x") is True
