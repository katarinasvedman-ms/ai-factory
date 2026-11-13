import json
from pathlib import Path

import pytest

from ai_factory.resolvers import LocalResolver


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    entries = []
    for name, technology, roles in (
        ("dotnet-development", "dotnet", ["coder", "tester"]),
        ("test-strategy", "*", ["tester"]),
        ("java-development", "java", ["coder", "tester"]),
    ):
        entry = {
            "id": name,
            "version": "1.0.0",
            "path": f"skills/{name}",
            "roles": roles,
            "technologies": [technology],
            "tasks": ["*"],
            "prerequisites": [],
            "dependencies": [],
            "compatibility": ["copilot-sdk-python-1"],
        }
        entries.append(entry)
        directory = root / entry["path"]
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use for {technology} tasks.\n---\n"
            "Read [guidance](references/guide.md) before returning a proposal.\n",
            encoding="utf-8",
        )
        (directory / "references").mkdir()
        (directory / "references/guide.md").write_text(
            "Preserve compatibility.\n", encoding="utf-8"
        )
    (root / "catalog.json").write_text(json.dumps({"schema_version": 1, "skills": entries}))
    return root


@pytest.fixture
def snapshot(library):
    return LocalResolver(library).resolve()


@pytest.fixture
def target(tmp_path):
    import shutil

    root = tmp_path / "orders"
    shutil.copytree(Path(__file__).parent / "fixtures/orders", root)
    return root


@pytest.fixture
def policy(snapshot):
    return {
        "allowed_skills": list(snapshot.entries),
        "required_skills": {"tester": ["test-strategy"]},
    }


@pytest.fixture
def request_data():
    return {
        "id": "test-001",
        "description": "Propose order validation",
        "repository": "local:orders",
    }
