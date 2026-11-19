from __future__ import annotations

import json
from pathlib import Path

import pytest

from dark_factory.adapters.definition.warp import (
    WarpDefinitionError,
    load_definition,
    public_report,
)
from dark_factory.cli import main
from dark_factory.config import ConfigError, parse_toml_bytes
from dark_factory.controller import Controller
from dark_factory.improvement import analyze_improvements
from dark_factory.store import Store
from dark_factory.worker import Worker


def warp_tree(tmp_path: Path, model: str = "auto") -> Path:
    root = tmp_path / "warp"
    (root / "agents" / "spec").mkdir(parents=True)
    (root / "agents" / "implement" / "skills" / "implementation").mkdir(parents=True)
    (root / "agents" / "review").mkdir(parents=True)
    (root / "skills" / "shared").mkdir(parents=True)
    (root / "scorers" / "task-compliance").mkdir(parents=True)
    (root / "runners").mkdir()
    (root / "automations" / "scheduled").mkdir(parents=True)
    (root / "factory.yaml").write_text(
        "\n".join(
            [
                "schemaVersion: v1alpha1",
                "name: portable-factory",
                "repositories:",
                "  - owner: yeutterg",
                "    name: dark-factory",
                "agentDefaults:",
                f"  model: {model}",
                "  runner: linux-standard",
            ]
        )
    )
    for name, agent_type, body in (
        ("spec", "SPEC", "Plan only the selected root outcome."),
        ("implement", "IMPLEMENT", "Implement the approved outcome."),
        ("review", "REVIEW", "Review the exact candidate independently."),
    ):
        (root / "agents" / name / "agent.md").write_text(
            f"---\nagentType: {agent_type}\n---\n{body}\n"
        )
    (root / "skills" / "shared" / "SKILL.md").write_text("# Shared\nUse evidence.\n")
    (
        root / "agents" / "implement" / "skills" / "implementation" / "SKILL.md"
    ).write_text("# Implementation\nKeep scope narrow.\n")
    (root / "scorers" / "task-compliance" / "scorer.md").write_text(
        """---
name: Task compliance
agents: [implement]
labels:
  - value: pass
    score: 1
  - value: fail
    score: 0
passingScore: 1
selfImprovement: true
---
Check whether the implementation delivered only the selected work.
"""
    )
    (root / "runners" / "linux-standard.yaml").write_text(
        "platform:\n  os: linux\n  arch: x86_64\n"
    )
    (root / "automations" / "scheduled" / "automation.md").write_text(
        "---\ntriggers:\n  - provider: schedule\n    event: scheduled\n---\nRun.\n"
    )
    return root


def configured(tmp_path: Path, root: Path):
    profile = Path("examples/factory.toml").resolve()
    text = profile.read_text()
    text = text.replace('state_dir = "./state"', f'state_dir = "{tmp_path / "state"}"')
    text += (
        f'\n[definition]\nformat = "warp-v1alpha1"\npath = "{root}"\nstrict = true\n'
    )
    return parse_toml_bytes(text.encode(), profile)


def test_warp_definition_maps_only_fixed_workflow_assets(tmp_path: Path) -> None:
    root = warp_tree(tmp_path)
    definition = load_definition(root)

    assert definition["repositories"] == ["yeutterg/dark-factory"]
    assert set(definition["role_prompts"]) == {"planner", "coder", "reviewer"}
    assert len(definition["shared_skills"]) == 1
    assert len(definition["role_skills"]["coder"]) == 1
    assert definition["scorers"][0]["status"] == "recognized-not-executed"
    assert definition["compatibility"]["unsupported_resources"] == {"automations": 1}
    assert any(
        "local execution profile remains authoritative" in warning
        for warning in definition["compatibility"]["warnings"]
    )
    report = public_report(definition)
    assert "root" not in report
    assert "role_prompts" not in report


def test_warp_definition_unknown_fields_fail_closed(tmp_path: Path) -> None:
    root = warp_tree(tmp_path)
    factory = root / "factory.yaml"
    factory.write_text(factory.read_text() + "\nmagicPermission: true\n")
    with pytest.raises(WarpDefinitionError, match="unknown factory.yaml fields"):
        load_definition(root)
    report = load_definition(root, strict=False)
    assert any(
        "magicPermission" in warning for warning in report["compatibility"]["warnings"]
    )


def test_warp_definition_rejects_ambiguous_yaml_and_symlinks(tmp_path: Path) -> None:
    root = warp_tree(tmp_path)
    factory = root / "factory.yaml"
    original = factory.read_text()
    factory.write_text(original + "\nname: replacement\n")
    with pytest.raises(WarpDefinitionError, match="duplicate YAML key"):
        load_definition(root)

    factory.write_text(original)
    outside = tmp_path / "outside-agent"
    outside.mkdir()
    (outside / "agent.md").write_text("---\nagentType: CUSTOM\n---\nOutside\n")
    (root / "agents" / "outside").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WarpDefinitionError, match="cannot be symlinks"):
        load_definition(root)


def test_profile_binds_definition_assets_and_digest(tmp_path: Path) -> None:
    root = warp_tree(tmp_path)
    cfg = configured(tmp_path, root)

    assert Path(cfg.prompts["planner"]).name == "agent.md"
    assert (
        Path(cfg.prompts["coder"])
        .read_text()
        .endswith("Implement the approved outcome.\n")
    )
    assert len(cfg.skills) == 3  # two packaged profile skills plus one shared skill
    assert len(cfg.role_skills["coder"]) == 1
    first_digest = cfg.digest
    implement = root / "agents" / "implement" / "agent.md"
    implement.write_text(implement.read_text() + "Be concise.\n")
    assert configured(tmp_path, root).digest != first_digest


def test_definition_model_request_cannot_expand_approved_route(tmp_path: Path) -> None:
    root = warp_tree(tmp_path, model="unapproved-model")
    with pytest.raises(ConfigError, match="approved local route pins"):
        configured(tmp_path, root)


def test_definition_is_in_approval_policy_and_role_skills_are_scoped(
    tmp_path: Path,
) -> None:
    root = warp_tree(tmp_path)
    cfg = configured(tmp_path, root)
    store = Store(cfg.state_dir)
    try:
        ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        policy = ctrl._policy()
        assert policy["definition"]["digest"] == cfg.definition["digest"]
        assert ctrl.status()["improvements"]["enabled"] is True
        assert [
            skill["name"] for skill in policy["assets"]["role_skills"]["coder"]
        ] == ["implementation"]
        assert not policy["assets"]["role_skills"].get("reviewer")
    finally:
        store.close()


def test_fixed_workflow_runs_with_warp_mapped_prompts_and_skills(
    tmp_path: Path,
) -> None:
    root = warp_tree(tmp_path)
    cfg = configured(tmp_path, root)
    store = Store(cfg.state_dir)
    try:
        ctrl = Controller(cfg, store, Path("src/dark_factory/agents/prompts"))
        wid = ctrl.intake_issues(
            [
                {
                    "repo": "yeutterg/dark-factory",
                    "number": 91,
                    "title": "Warp-compatible fixture",
                    "body": "Exercise the existing fixed workflow.",
                    "is_root": True,
                }
            ]
        )[0]
        ctrl.start_planning(wid)
        worker = Worker(cfg, store, ctrl, "definition-fixture")
        assert worker.run_loop(2) == 2
        ctrl.approve(wid, "human-fixture")
        assert worker.run_loop() == 3
        row = store.one(
            "SELECT status,snapshot_json FROM work_items WHERE id=?", (wid,)
        )
        assert row["status"] == "awaiting_review"
        snapshot = json.loads(row["snapshot_json"])
        assert snapshot["policy"]["definition"]["digest"] == cfg.definition["digest"]
        assert snapshot["policy"]["assets"]["role_skills"]["coder"][0]["name"] == (
            "implementation"
        )
    finally:
        store.close()


def test_definition_validate_cli_needs_no_controller_profile(
    tmp_path: Path, capsys
) -> None:
    root = warp_tree(tmp_path)
    assert main(["definition-validate", str(root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["compatibility"]["syntax"] == "compatible"


def test_improvement_analysis_is_default_on_and_proposal_only(tmp_path: Path) -> None:
    root = warp_tree(tmp_path)
    cfg = configured(tmp_path, root)
    store = Store(cfg.state_dir)
    try:
        for index in range(3):
            root_id = f"wi_{index}"
            store.execute(
                """INSERT INTO work_items
                (id,external_id,url,title,body,repo,status,is_root,created_at)
                VALUES (?,?,?,?,?,?,?,1,?)""",
                (
                    root_id,
                    f"fixture:{index}",
                    "",
                    f"Outcome {index}",
                    "",
                    "yeutterg/dark-factory",
                    "rejected",
                    f"2026-11-19T00:00:0{index}+00:00",
                ),
            )
            store.audit(
                "human",
                "reject",
                {"work_item_id": root_id, "reason": "The plan omitted a requirement"},
            )

        before = len(store.query("SELECT * FROM work_items"))
        report = analyze_improvements(store, cfg)
        assert report["enabled"] is True
        assert report["mode"] == "proposal-only"
        assert len(report["proposals"]) == 1
        assert report["proposals"][0]["signal"] == "human-rejection"
        assert report["external_actions"] == []
        assert len(store.query("SELECT * FROM work_items")) == before
    finally:
        store.close()
