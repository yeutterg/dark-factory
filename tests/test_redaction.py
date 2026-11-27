from dark_factory.redaction import MASK, redact


def test_exact_values_and_credential_shapes_are_masked():
    text = "key=hunter2-secret-value and ghp_" + "A" * 36 + " and sk-ant-" + "b" * 40
    out = redact(text, ["hunter2-secret-value"])
    assert "hunter2-secret-value" not in out
    assert "ghp_" not in out and "sk-ant-" not in out
    assert out.count(MASK) == 3


def test_short_values_are_not_treated_as_secrets_and_none_is_safe():
    assert redact("a b c", ["a"]) == "a b c"
    assert redact(None) == ""
    assert redact("", ["longsecretvalue"]) == ""


def test_the_worker_masks_a_credential_the_harness_echoes(tmp_path, monkeypatch):
    import json

    from test_workflow import setup

    from dark_factory.adapters.harness.base import HarnessEvent
    from dark_factory.adapters.harness.fake import FakeHarness
    from dark_factory.worker import Worker

    leaked = "sk-ant-" + "z" * 40

    class Leaky(FakeHarness):
        def start(self, prompt, workspace, extra=None):
            yield HarnessEvent("text", f"found {leaked} in the environment")
            yield from super().start(prompt, workspace, extra)

    monkeypatch.setattr(Worker, "_harness", lambda self, route: Leaky())
    cfg, store, ctrl, wid, worker = setup(tmp_path)
    worker.run_loop(2)
    for archive in (cfg.state_dir / "archives").iterdir():
        events = archive / "events.jsonl"
        if events.exists():
            assert leaked not in events.read_text()
            assert json.dumps(leaked) not in events.read_text()
