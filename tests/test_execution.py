import json
import stat

import pytest

from ai_factory.contracts import FactoryError
from ai_factory.execution import prepare, verify
from ai_factory.profile import profile, repository_files
from ai_factory.resolvers import LocalResolver
from ai_factory.selection import select


def test_profile_uses_evidence_without_running_code(target):
    (target / "setup.py").write_text("raise Exception('must not execute')")
    result = profile(repository_files(target))
    assert result["technologies"] == ["api", "dotnet", "python", "sqlserver"]
    assert result["evidence"]["sqlserver"]
    assert profile({"README.md": b"we might use Java one day"})["status"] == "unknown"


def test_role_selection_and_required_skill_policy(snapshot, policy):
    assignments = select(
        snapshot,
        ["dotnet"],
        ["coder", "tester"],
        ["implementation"],
        policy["allowed_skills"],
        policy["required_skills"],
    )
    assert assignments == {
        "coder": ["dotnet-development"],
        "tester": ["dotnet-development", "test-strategy"],
    }
    with pytest.raises(FactoryError, match="unauthorized"):
        select(
            snapshot,
            ["dotnet"],
            ["tester"],
            ["testing"],
            ["dotnet-development"],
            {"tester": ["test-strategy"]},
        )


def test_dependency_closure_cannot_bypass_role_or_allowlist(snapshot):
    snapshot.entries["dotnet-development"]["dependencies"] = ["test-strategy"]
    with pytest.raises(FactoryError, match="not authorized for role"):
        select(snapshot, ["dotnet"], ["coder"], ["implementation"], list(snapshot.entries))
    snapshot.entries["test-strategy"]["roles"].append("coder")
    with pytest.raises(FactoryError, match="unauthorized"):
        select(snapshot, ["dotnet"], ["coder"], ["implementation"], ["dotnet-development"])


@pytest.mark.parametrize("defect", ["cycle", "missing", "frontmatter", "resource", "duplicate"])
def test_invalid_snapshots_fail_closed(library, defect):
    catalog = json.loads((library / "catalog.json").read_text())
    if defect == "cycle":
        catalog["skills"][0]["dependencies"] = ["dotnet-development"]
    elif defect == "missing":
        catalog["skills"][0]["dependencies"] = ["missing"]
    elif defect == "frontmatter":
        (library / "skills/dotnet-development/SKILL.md").write_text("Not a skill")
    elif defect == "resource":
        (library / "skills/dotnet-development/references/guide.md").unlink()
    else:
        catalog["skills"].append(catalog["skills"][0])
    (library / "catalog.json").write_text(json.dumps(catalog))
    with pytest.raises(FactoryError):
        LocalResolver(library).resolve()


def test_execution_is_pinned_and_resumes_without_source(
    target, library, snapshot, policy, request_data, tmp_path
):
    a = prepare(
        target,
        snapshot,
        tmp_path / "runs",
        request_data,
        policy,
        ["coder", "tester"],
        ["implementation"],
    )
    before = verify(a)
    resource = library / "skills/dotnet-development/references/guide.md"
    resource.write_text("New release instructions.\n")
    b = prepare(
        target,
        LocalResolver(library).resolve(),
        tmp_path / "runs",
        request_data,
        policy,
        ["coder", "tester"],
        ["implementation"],
    )
    resource.unlink()
    assert verify(a) == before
    assert (
        verify(b)["skills"]["dotnet-development"]["sha256"]
        != (before["skills"]["dotnet-development"]["sha256"])
    )
    assert a != b
    assert before["assignments"]["coder"] == ["dotnet-development"]
    assert before["skills"]["dotnet-development"]["version"] == "1.0.0"
    assert (a / "skills/coder/dotnet-development/SKILL.md").read_bytes() == (
        a / "skills/tester/dotnet-development/SKILL.md"
    ).read_bytes()


@pytest.mark.parametrize("change", ["resource", "target", "extra-skill", "request"])
def test_resume_detects_tampering(target, snapshot, policy, request_data, tmp_path, change):
    directory = prepare(
        target,
        snapshot,
        tmp_path / "runs",
        request_data,
        policy,
        ["coder", "tester"],
        ["implementation"],
    )
    if change == "resource":
        path = directory / "skills/coder/dotnet-development/references/guide.md"
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
        path.write_text("Tampered")
    elif change == "target":
        path = directory / "target/OrderService.cs"
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
        path.write_text("Tampered")
    elif change == "extra-skill":
        (directory / "skills/coder/unauthorized").mkdir()
    else:
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["request"]["description"] = "Different request"
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(FactoryError):
        verify(directory)


def test_unknown_stack_uses_generic_skills(snapshot):
    assert select(snapshot, [], ["tester"], ["testing"], list(snapshot.entries)) == {
        "tester": ["test-strategy"]
    }


@pytest.mark.parametrize("defect", ["runtime", "prerequisite"])
def test_compatibility_and_prerequisites_are_enforced(snapshot, defect):
    if defect == "runtime":
        snapshot.entries["dotnet-development"]["compatibility"] = ["other-runtime"]
    else:
        snapshot.entries["dotnet-development"]["prerequisites"] = ["java"]
    with pytest.raises(FactoryError):
        select(snapshot, ["dotnet"], ["coder"], ["implementation"], list(snapshot.entries))
