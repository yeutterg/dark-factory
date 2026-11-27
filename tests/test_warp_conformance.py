"""The reader accepts what Warp's published schemas accept, and rejects what they reject.

Schemas and example trees are vendored test data (tests/fixtures/warp/PROVENANCE.md).
`jsonschema` is a development dependency only; the runtime reader is hand-written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema")
from referencing import Registry, Resource  # noqa: E402

from dark_factory.adapters.definition.warp import (  # noqa: E402
    WarpDefinitionError,
    load_definition,
)

FIXTURES = Path(__file__).parent / "fixtures" / "warp"
SCHEMAS = FIXTURES / "schemas"
TREES = sorted(p for p in (FIXTURES / "examples").iterdir() if p.is_dir())

_registry = Registry().with_resource(
    "common.schema.json",
    Resource.from_contents(json.loads((SCHEMAS / "common.schema.json").read_text())),
)


def validator(name):
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema, registry=_registry)


def frontmatter(path: Path) -> dict:
    lines = path.read_text().splitlines()
    end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    return yaml.safe_load("\n".join(lines[1:end])) or {}


def schema_errors(tree: Path) -> list[str]:
    """Every schema violation in a tree, by file."""
    errors = []

    def check(name, document, path):
        for error in validator(name).iter_errors(document):
            errors.append(f"{path.relative_to(tree)}: {error.message}")

    check(
        "factory",
        yaml.safe_load((tree / "factory.yaml").read_text()),
        tree / "factory.yaml",
    )
    for kind, name, source in (
        ("agents", "agent", "agent.md"),
        ("scorers", "scorer", "scorer.md"),
        ("automations", "automation", "automation.md"),
    ):
        for path in (
            sorted((tree / kind).glob(f"*/{source}")) if (tree / kind).exists() else []
        ):
            document = frontmatter(path)
            if name == "scorer":
                # Warp takes a scorer's name from its directory when it is omitted.
                document.setdefault("name", path.parent.name)
            check(name, document, path)
    for kind, name in (
        ("runners", "runner"),
        ("routers", "router"),
        ("webhooks", "webhook"),
    ):
        for path in (
            sorted((tree / kind).glob("*.y*ml")) if (tree / kind).exists() else []
        ):
            check(name, yaml.safe_load(path.read_text()), path)
    return errors


def test_the_repositorys_own_example_is_schema_valid_and_loads():
    tree = Path(__file__).parents[1] / "examples" / "warp-factory"
    assert schema_errors(tree) == []
    assert load_definition(tree, strict=True)["compatibility"]["mapped_roles"] == [
        "coder",
        "planner",
        "reviewer",
    ]


@pytest.mark.parametrize("tree", TREES, ids=lambda p: p.name)
def test_every_published_example_is_schema_valid_and_loads(tree):
    assert schema_errors(tree) == []  # the vendored data itself is sound
    definition = load_definition(tree, strict=True)
    assert definition["schema_version"] == "v1alpha1"
    assert definition["files"], "the digest must cover the whole tree"


@pytest.mark.parametrize("tree", TREES, ids=lambda p: p.name)
def test_digest_covers_every_file_in_the_tree(tree):
    definition = load_definition(tree, strict=True)
    on_disk = {
        p.relative_to(tree).as_posix()
        for p in tree.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }
    assert set(definition["files"]) == on_disk


def copy_tree(source: Path, destination: Path) -> Path:
    import shutil

    shutil.copytree(source, destination)
    return destination


def test_unknown_agent_field_is_rejected_like_the_schema(tmp_path):
    tree = copy_tree(
        FIXTURES / "examples" / "01-single-repo-quickstart", tmp_path / "t"
    )
    agent = next((tree / "agents").glob("*/agent.md"))
    text = agent.read_text()
    agent.write_text(text.replace("---\n", "---\nunknownField: 1\n", 1))
    assert schema_errors(tree)
    with pytest.raises(WarpDefinitionError):
        load_definition(tree, strict=True)


def test_missing_repositories_key_is_rejected(tmp_path):
    tree = copy_tree(
        FIXTURES / "examples" / "01-single-repo-quickstart", tmp_path / "t"
    )
    factory = yaml.safe_load((tree / "factory.yaml").read_text())
    factory.pop("repositories")
    (tree / "factory.yaml").write_text(yaml.safe_dump(factory))
    with pytest.raises(WarpDefinitionError, match="repositories"):
        load_definition(tree, strict=True)


def test_v1alpha2_and_workers_are_reported_as_unsupported_versions(tmp_path):
    tree = copy_tree(
        FIXTURES / "examples" / "01-single-repo-quickstart", tmp_path / "t"
    )
    factory = yaml.safe_load((tree / "factory.yaml").read_text())
    factory["schemaVersion"] = "v1alpha2"
    (tree / "factory.yaml").write_text(yaml.safe_dump(factory))
    with pytest.raises(WarpDefinitionError, match="v1alpha2.*not supported"):
        load_definition(tree, strict=True)
    factory["schemaVersion"] = "v1alpha1"
    (tree / "factory.yaml").write_text(yaml.safe_dump(factory))
    (tree / "workers").mkdir()
    (tree / "workers" / "w.md").write_text("---\nname: w\n---\nbody\n")
    with pytest.raises(WarpDefinitionError, match="workers.*not supported"):
        load_definition(tree, strict=True)


def test_agent_type_must_be_uppercase_and_null_means_custom(tmp_path):
    tree = copy_tree(
        FIXTURES / "examples" / "01-single-repo-quickstart", tmp_path / "t"
    )
    agent = next((tree / "agents").glob("*/agent.md"))
    text = agent.read_text()
    assert "agentType:" in text
    lowered = text.replace("agentType: SPEC", "agentType: spec").replace(
        "agentType: IMPLEMENT", "agentType: implement"
    )
    if lowered != text:
        agent.write_text(lowered)
        with pytest.raises(WarpDefinitionError, match="agentType"):
            load_definition(tree, strict=True)
