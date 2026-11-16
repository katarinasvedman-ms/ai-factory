import base64
import copy
import json
import shutil
from types import SimpleNamespace

import pytest
from test_provider import toolbox as toolbox

from ai_factory import skill_evaluation as evaluation
from ai_factory.contracts import CLI_VERSION, FactoryError, digest
from ai_factory.execution import write_json
from ai_factory.provider import ToolboxSkillProvider


@pytest.fixture
def prepared(tmp_path_factory, target, snapshot, toolbox, monkeypatch):
    tmp_path = tmp_path_factory.mktemp("sel")
    root = tmp_path / "consumer"
    for folder in ("src", "tools", "tests", "examples"):
        (root / folder).mkdir(parents=True)
    shutil.copytree(target, root / "tests" / "target")
    release = {
        "roles": {
            "coder": {
                **toolbox.binding,
                "skills": {name: "1" for name in toolbox.records},
                "identity": {"principal_id": "historical-must-not-be-adopted"},
            }
        },
        "release": {
            "source_commit": "a" * 40,
            "skills": {
                name: {"foundry_version": "1", "files": record["files"]}
                for name, record in snapshot.records.items()
            },
        },
    }
    release_path = tmp_path / "binding.json"
    write_json(release_path, release)
    cases = [
        {
            "id": f"S{i}",
            "fixture": "tests/target",
            "task": "Propose focused input validation and describe regression cases.",
            "expected": ["dotnet-development"],
            "optional": [],
            "resources": [{"skill": "dotnet-development", "path": "references/guide.md"}],
            "proposal_paths": ["OrderService.cs"],
            "proposal_required": True,
            "obligations": ["Preserves valid input and rejects invalid input."],
        }
        for i in range(1, 7)
    ]
    matrix = {
        "schema_version": 1,
        "role": "coder",
        "repetitions": 3,
        "release": {
            "tag": "test-release",
            "source_commit": "a" * 40,
            "binding_sha256": digest(release_path.read_bytes()),
        },
        "cases": cases,
    }
    matrix_path, evaluations_path = tmp_path / "matrix.json", tmp_path / "evaluations.json"
    write_json(evaluations_path, [{"skill": name} for name in snapshot.records])
    matrix["evaluation_sha256"] = digest(evaluations_path.read_bytes())
    write_json(matrix_path, matrix)
    monkeypatch.setattr(
        evaluation.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(
            returncode=0, stdout="" if "status" in args else "b" * 40
        ),
    )
    directory = evaluation.prepare_evaluation(
        matrix_path, release_path, evaluations_path, root, tmp_path / "outputs", "test-model"
    )
    return SimpleNamespace(
        directory=directory,
        matrix=matrix,
        matrix_path=matrix_path,
        release=release,
        release_path=release_path,
        evaluations_path=evaluations_path,
        root=root,
    )


class EvidenceClient:
    def __init__(self, evidence, **options):
        self.evidence = evidence

    async def start(self):
        pass

    async def stop(self):
        pass

    async def get_auth_status(self):
        return SimpleNamespace(isAuthenticated=True)

    async def get_status(self):
        return SimpleNamespace(version=CLI_VERSION)

    async def list_models(self):
        return [SimpleNamespace(id="test-model")]

    async def create_session(self, **config):
        provider = config["skill_provider"]
        await provider.list_skills()
        evidence = self.evidence

        class Session:
            async def send_and_wait(self, prompt, timeout):
                assert "expected" not in json.loads(prompt)["request"]
                await provider.read_skill("dotnet-development")
                native = {
                    "type": "skill.invoked",
                    "data": {"name": "dotnet-development", "source": "sdk"},
                }
                config["on_event"](
                    SimpleNamespace(
                        type=SimpleNamespace(value="skill.invoked"),
                        data=SimpleNamespace(name="dotnet-development"),
                        to_dict=lambda: native,
                    )
                )
                tools = {tool.name: tool for tool in config["tools"]}
                read = tools["read_skill_resource"].handler(
                    SimpleNamespace(
                        arguments={
                            "skill": "dotnet-development",
                            "path": "references/guide.md",
                        }
                    )
                )
                assert read.result_type != "failure"
                manifest = json.loads((evidence / "manifest.json").read_text())
                original = (evidence / "target/OrderService.cs").read_text()
                proposal = tools["propose_file"].handler(
                    SimpleNamespace(
                        arguments={
                            "path": "OrderService.cs",
                            "base_sha256": manifest["target"]["files"]["OrderService.cs"],
                            "content": original + "\n// proposed change\n",
                        }
                    )
                )
                assert proposal.result_type != "failure"
                return SimpleNamespace(data=SimpleNamespace(content="Proposed tests; NOT RUN."))

            async def disconnect(self):
                pass

        return Session()


def test_pinned_configuration_ignores_publisher_identity_and_does_not_run(prepared):
    config = evaluation.configuration_for(prepared.directory)
    assert "identity" not in config["consumer_binding"]
    assert "required_skills" not in config["policy"]
    assert len(config["policy"]["permitted_skills"]["coder"]) == 3
    assert evaluation.load(prepared.directory / "results.json") == []
    assert "incomplete" in (prepared.directory / "report.md").read_text()
    assert not (prepared.directory / "runs").exists()


@pytest.mark.parametrize(
    "defect", ["binding", "source", "expected", "fixture", "definitions", "coverage"]
)
def test_invalid_release_or_matrix_refused_before_execution(prepared, defect, tmp_path):
    matrix = copy.deepcopy(prepared.matrix)
    if defect == "binding":
        matrix["release"]["binding_sha256"] = "0" * 64
    elif defect == "source":
        matrix["release"]["source_commit"] = "0" * 40
    elif defect == "expected":
        matrix["cases"][0]["expected"] = ["not-in-release"]
    elif defect == "fixture":
        matrix["cases"][0]["fixture"] = "../outside"
    else:
        write_json(prepared.evaluations_path, [{"skill": "unknown"}])
        if defect == "coverage":
            matrix["evaluation_sha256"] = digest(prepared.evaluations_path.read_bytes())
    write_json(prepared.matrix_path, matrix)
    with pytest.raises(FactoryError):
        evaluation.prepare_evaluation(
            prepared.matrix_path,
            prepared.release_path,
            prepared.evaluations_path,
            prepared.root,
            tmp_path / "other",
            "test-model",
        )


def test_live_requires_explicit_approval_and_fixed_source(prepared):
    with pytest.raises(FactoryError, match="Both"):
        evaluation.execute_evaluation(prepared.directory, live=True, approve_rubric=False)
    (prepared.root / "src/changed.py").write_text("print('changed')")
    with pytest.raises(FactoryError, match="Consumer source changed"):
        evaluation.execute_evaluation(prepared.directory, live=True, approve_rubric=True)
    assert not (prepared.directory / "approval.json").exists()


def test_bounded_run_uses_native_evidence_without_claiming_semantic_success(prepared, toolbox):
    evaluation.execute_evaluation(
        prepared.directory,
        live=True,
        approve_rubric=True,
        client_factory=toolbox.factory,
        observed_factory=EvidenceClient,
    )
    rows = evaluation.load(prepared.directory / "results.json")
    assert len(rows) == 18
    assert all(row["deterministic_pass"] for row in rows)
    assert all(row["activated"] == ["dotnet-development"] for row in rows)
    assert all(row["guidance_use"] == "uncertain" for row in rows)
    summary = evaluation.load(prepared.directory / "summary.json")
    assert summary["passed"] == summary["reviewed"] == 0
    assert "incomplete" in summary["recommendation"]
    with pytest.raises(FactoryError, match="already has attempts"):
        evaluation.execute_evaluation(
            prepared.directory,
            live=True,
            approve_rubric=True,
            client_factory=toolbox.factory,
        )
    assert not (prepared.directory / ".evaluation.lock").exists()

    reviews = evaluation.load(prepared.directory / "reviews.json")
    for item in reviews:
        item.update(
            status="pass",
            reviewer="test-reviewer",
            notes="Verified fixture-specific output obligations against the recorded artifacts.",
            evidence=["coder.result.json"],
        )
    write_json(prepared.directory / "reviews.json", reviews)
    evaluation.render_report(prepared.directory)
    assert evaluation.load(prepared.directory / "summary.json")["passed"] == 18
    assert "Keep native selection" in (prepared.directory / "report.md").read_text()

    config = evaluation.configuration_for(prepared.directory)
    case = copy.deepcopy(config["matrix"]["cases"][0])
    case["expected"].append("java-development")
    assert evaluation.score_attempt(prepared.directory, rows[0], config, case)["missing"] == [
        "java-development"
    ]
    case["expected"] = []
    assert evaluation.score_attempt(prepared.directory, rows[0], config, case)["unnecessary"] == [
        "dotnet-development"
    ]
    case["resources"][0]["path"] = "references/not-read.md"
    assert (
        evaluation.score_attempt(prepared.directory, rows[0], config, case)["missing_resources"]
        == case["resources"]
    )

    reviews[0]["evidence_sha256"] = "0" * 64
    write_json(prepared.directory / "reviews.json", reviews)
    with pytest.raises(FactoryError, match="stale"):
        evaluation.render_report(prepared.directory)
    review = reviews[0]
    review["evidence_sha256"] = rows[0]["evidence_sha256"]
    write_json(prepared.directory / "reviews.json", reviews)
    evidence = prepared.directory / rows[0]["evidence"]
    proposal_path = next((evidence / "proposals").glob("*.json"))
    proposal = evaluation.load(proposal_path)
    proposal["status"] = "approved"
    write_json(proposal_path, proposal)
    assert not evaluation.score_attempt(
        prepared.directory, rows[0], config, config["matrix"]["cases"][0]
    )["guardrails"]["proposal_integrity"]
    with pytest.raises(FactoryError, match="artifacts changed"):
        evaluation.render_report(prepared.directory)


@pytest.mark.parametrize("defect", ["unauthorized", "pin-tamper"])
async def test_release_hash_and_allowlist_fail_closed(toolbox, snapshot, defect):
    manifest = {
        "distribution": toolbox.source,
        "assignments": {"coder": ["dotnet-development"]},
        "skills": {
            name: {**record, **snapshot.records[name]} for name, record in toolbox.records.items()
        },
    }
    if defect == "pin-tamper":
        manifest["skills"]["dotnet-development"]["files"] = {"SKILL.md": "0" * 64}
    provider = ToolboxSkillProvider(manifest, "coder", client_factory=toolbox.factory)
    try:
        await provider.list_skills()
        with pytest.raises(FactoryError, match="not permitted|pinned skill bytes"):
            await provider.read_skill(
                "java-development" if defect == "unauthorized" else "dotnet-development"
            )
        assert not provider.loaded
    finally:
        await provider.aclose()
    assert not provider.root.exists()


def test_failed_attempts_are_retained_without_leaking_exception_text(prepared, toolbox):
    async def failing(*args, **kwargs):
        raise TimeoutError("do-not-persist-credential-like-exception-content")

    evaluation.execute_evaluation(
        prepared.directory,
        live=True,
        approve_rubric=True,
        client_factory=toolbox.factory,
        observed_factory=EvidenceClient,
        runner=failing,
    )
    rows = evaluation.load(prepared.directory / "results.json")
    assert len(rows) == 18
    assert all(row["failure_class"] == "timeout" for row in rows)
    assert not any(row["deterministic_pass"] for row in rows)
    assert "do-not-persist" not in (prepared.directory / "results.json").read_text()
    summary = evaluation.load(prepared.directory / "summary.json")
    assert summary["accounted"] == 18 and summary["completed"] == 0
    assert summary["output_reviews_complete"]
    assert "infrastructure failures" in summary["recommendation"]


def test_semantic_review_needs_real_evidence_and_no_external_paths(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run/output.json").write_text("{}")
    row = {
        "case": "S1",
        "repeat": 1,
        "evidence": "run",
        "evidence_sha256": evaluation.evidence_hash(tmp_path / "run"),
        "deterministic_pass": True,
    }
    review = {
        "case": "S1",
        "repeat": 1,
        "status": "pass",
        "evidence_sha256": row["evidence_sha256"],
        "reviewer": "reviewer",
        "notes": "Reviewed source-grounded behavior.",
        "evidence": ["../outside.json"],
    }
    with pytest.raises(FactoryError):
        evaluation.reviewed_results(tmp_path, [row], [review])
    review["evidence"] = ["output.json"]
    assert evaluation.reviewed_results(tmp_path, [row], [review])[0]["case_pass"]
    (tmp_path / "run/output.json").write_text('{"tampered": true}')
    with pytest.raises(FactoryError, match="artifacts changed"):
        evaluation.reviewed_results(tmp_path, [row], [review])


def test_configuration_hash_detects_changed_policy(prepared):
    path = prepared.directory / "configuration.json"
    config = evaluation.load(path)
    config["policy"]["required_skills"] = {"coder": ["dotnet-development"]}
    write_json(path, config)
    with pytest.raises(FactoryError, match="configuration changed"):
        evaluation.configuration_for(prepared.directory)


def test_consumer_identity_is_explicit_not_adopted_from_publication(prepared, tmp_path):
    identity = {"tenant_id": "consumer-tenant", "client_id": "consumer-app", "principal_id": "oid"}
    directory = evaluation.prepare_evaluation(
        prepared.matrix_path,
        prepared.release_path,
        prepared.evaluations_path,
        prepared.root,
        tmp_path / "identity",
        "test-model",
        identity,
    )
    config = evaluation.configuration_for(directory)
    assert config["consumer_binding"]["identity"] == identity
    assert "historical-must-not-be-adopted" not in json.dumps(config)


def test_fetch_preserves_exact_release_and_definition_bytes(prepared, monkeypatch, tmp_path):
    prepared.matrix["release"]["repository"] = "example/skills"
    prepared.matrix["evaluation_source"] = "evaluations.json"
    write_json(prepared.matrix_path, prepared.matrix)
    binding = prepared.release_path.read_bytes()
    definitions = prepared.evaluations_path.read_bytes()
    replies = iter(
        [
            json.dumps({"assets": [{"name": "role-bindings.json", "id": 123}]}).encode(),
            binding,
            json.dumps({"content": base64.b64encode(definitions).decode()}).encode(),
        ]
    )
    calls = []

    def gh(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=next(replies))

    monkeypatch.setattr(evaluation.subprocess, "run", gh)
    output = tmp_path / "inputs"
    evaluation.fetch_inputs(prepared.matrix_path, output)
    assert (output / "role-bindings.json").read_bytes() == binding
    assert (output / "evaluations.json").read_bytes() == definitions
    assert "ref=" + "a" * 40 in calls[-1][-1]
    assert calls[1][-1] == "Accept: application/octet-stream"


def test_unavailable_model_aborts_before_any_attempt(prepared, toolbox):
    class Unavailable(EvidenceClient):
        async def list_models(self):
            return []

    with pytest.raises(FactoryError, match="Preflight failed"):
        evaluation.execute_evaluation(
            prepared.directory,
            live=True,
            approve_rubric=True,
            client_factory=toolbox.factory,
            observed_factory=Unavailable,
        )
    assert evaluation.load(prepared.directory / "results.json") == []
    assert evaluation.load(prepared.directory / "preflight.json")["status"] == "failed"
    assert not (prepared.directory / "runs").exists()


def test_no_activation_is_reported_as_runtime_policy_not_selection(prepared, toolbox):
    async def no_activation(*args, **kwargs):
        raise FactoryError("No native skill activation was observed")

    evaluation.execute_evaluation(
        prepared.directory,
        live=True,
        approve_rubric=True,
        client_factory=toolbox.factory,
        observed_factory=EvidenceClient,
        runner=no_activation,
    )
    rows = evaluation.load(prepared.directory / "results.json")
    assert len(rows) == 18
    assert all(row["failure_class"] == "mandatory-activation-policy" for row in rows)
    assert not any(row["selection_scored"] or row["deterministic_pass"] for row in rows)


@pytest.fixture
def comparison(prepared):
    spec = {
        "schema_version": 2,
        "baseline_matrix": prepared.matrix_path.name,
        "baseline_matrix_sha256": digest(prepared.matrix_path.read_bytes()),
        "tasks": {
            "S3": (
                "Review the existing endpoint and describe input validation and regression cases."
            ),
            "S4": "Propose focused normalization validation using the existing source conventions.",
        },
    }
    spec_path = prepared.matrix_path.with_name("comparison.json")
    write_json(spec_path, spec)
    directory = evaluation.prepare_evaluation(
        spec_path,
        prepared.release_path,
        prepared.evaluations_path,
        prepared.root,
        prepared.root.parent / "c",
        "test-model",
    )
    return SimpleNamespace(directory=directory, spec_path=spec_path, spec=spec, prepared=prepared)


def test_comparison_freezes_matched_prompts_rubric_pins_and_order(comparison):
    config = evaluation.configuration_for(comparison.directory)
    matrix = config["matrix"]
    assert matrix["schema_version"] == 2 and len(matrix["cases"]) == 4
    assert config["skill_versions"] == comparison.prepared.release["roles"]["coder"]["skills"]
    assert "required_skills" not in config["policy"]
    for scenario in ("S3", "S4"):
        paired = [case for case in matrix["cases"] if case["scenario"] == scenario]
        assert {case["arm"] for case in paired} == {"baseline", "all-relevant"}
        visible = [
            {key: value for key, value in case.items() if key not in {"id", "arm"}}
            for case in paired
        ]
        assert visible[0] == visible[1]
    assert [item["case"] for item in config["schedule"]] == [
        "C3",
        "T3",
        "C4",
        "T4",
        "T4",
        "C4",
        "T3",
        "C3",
        "C3",
        "T3",
        "C4",
        "T4",
    ]
    assert "all relevant skills" in config["skill_selection_instructions"]["all-relevant"]
    assert evaluation.load(comparison.directory / "summary.json")["planned"] == 12


def test_comparison_refuses_changed_baseline_or_unmatched_arms(comparison):
    prepared = comparison.prepared
    matrix = evaluation.matrix_for(comparison.spec_path)
    matrix["cases"][1]["optional"].append("java-development")
    with pytest.raises(FactoryError, match="differ beyond"):
        evaluation.validate_matrix(matrix, prepared.release, prepared.root)
    prepared.matrix_path.write_text("{}")
    with pytest.raises(FactoryError, match="baseline matrix changed"):
        evaluation.matrix_for(comparison.spec_path)


def test_comparison_runs_twelve_fresh_sessions_without_revealing_arms(comparison, toolbox):
    observed = []

    class CapturedClient(EvidenceClient):
        async def create_session(self, **config):
            session = await super().create_session(**config)

            class CapturedSession:
                async def send_and_wait(self, prompt, timeout):
                    observed.append(
                        {
                            "request": json.loads(prompt),
                            "system": config["system_message"]["content"],
                            "available_tools": config["available_tools"],
                        }
                    )
                    return await session.send_and_wait(prompt, timeout)

                async def disconnect(self):
                    await session.disconnect()

            return CapturedSession()

    evaluation.execute_evaluation(
        comparison.directory,
        live=True,
        approve_rubric=True,
        client_factory=toolbox.factory,
        observed_factory=CapturedClient,
    )
    rows = evaluation.load(comparison.directory / "results.json")
    assert len(rows) == len(observed) == 12
    assert len({row["evidence"] for row in rows}) == 12
    assert all(row["deterministic_pass"] for row in rows)
    assert [row["order"] for row in rows] == list(range(1, 13))
    for scenario in ("S3", "S4"):
        requests = [
            item["request"] for item in observed if item["request"]["request"]["id"] == scenario
        ]
        assert len(requests) == 6 and all(item == requests[0] for item in requests)
    assert all(item["available_tools"] == observed[0]["available_tools"] for item in observed)
    assert sum("load all relevant skills" in item["system"] for item in observed) == 6
    summary = evaluation.load(comparison.directory / "summary.json")
    assert summary["planned"] == summary["accounted"] == summary["completed"] == 12
    assert [arm["complete_relevant_sets"] for arm in summary["comparison"]] == [6, 6]
    assert [arm["guidance_pass"] for arm in summary["comparison"]] == [0, 0]


def test_wrapped_provider_timeout_is_normalized_without_rewriting_attempt(tmp_path):
    evidence = tmp_path / "run"
    evidence.mkdir()
    write_json(
        evidence / "coder.provider-metrics.json",
        {"failure": "Toolbox skill provider list failed for role coder: TimeoutError"},
    )
    row = {
        "case": "C3",
        "repeat": 1,
        "status": "failed",
        "failure_class": "runtime-or-policy",
        "deterministic_pass": False,
        "evidence": "run",
        "evidence_sha256": evaluation.evidence_hash(evidence),
    }
    result = evaluation.reviewed_results(tmp_path, [row], [])[0]
    assert result["failure_class"] == "timeout"
    assert result["recorded_failure_class"] == row["failure_class"] == "runtime-or-policy"
    assert not result["case_pass"]
