from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import os
import re
import subprocess
import time
import uuid
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from copilot import CopilotClient

from .contracts import (
    CLI_VERSION,
    NAME,
    SDK_VERSION,
    FactoryError,
    canonical,
    digest,
    relative_path,
)
from .execution import prepare, verify, write_json
from .profile import profile, repository_files
from .provider import ToolboxClient, ToolboxProviderResolver, ToolboxSkillProvider
from .runtime import SKILL_SELECTION_INSTRUCTIONS, _runtime_environment, run


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_hashes(root: Path) -> dict:
    return {name: digest(data) for name, data in sorted(repository_files(root).items())}


def consumer_hashes(root: Path) -> dict:
    files = {}
    for folder in ("src", "tools", "tests", "examples"):
        files.update(
            {f"{folder}/{name}": value for name, value in file_hashes(root / folder).items()}
        )
    for name in ("pyproject.toml", "requirements.lock", "uv.lock"):
        if (root / name).is_file():
            files[name] = digest((root / name).read_bytes())
    return files


def checked_fixture(root: Path, name: str) -> Path:
    relative_path(name)
    target = root / name
    if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
        raise FactoryError("Fixture must remain inside the consumer repository")
    if not target.is_dir():
        raise FactoryError("Fixture directory does not exist")
    return target


def matrix_for(path: Path) -> dict:
    matrix = load(path)
    if matrix.get("schema_version") != 2:
        return matrix
    relative_path(matrix["baseline_matrix"])
    baseline_path = path.parent / matrix["baseline_matrix"]
    if (
        not baseline_path.resolve().is_relative_to(path.parent.resolve())
        or baseline_path.is_symlink()
        or digest(baseline_path.read_bytes()) != matrix["baseline_matrix_sha256"]
    ):
        raise FactoryError("Comparison baseline matrix changed or is outside its directory")
    baseline = load(baseline_path)
    if baseline["schema_version"] != 1 or set(matrix["tasks"]) != {"S3", "S4"}:
        raise FactoryError("Comparison requires exactly the API and Python baseline scenarios")
    cases = []
    for scenario in ("S3", "S4"):
        source = next(case for case in baseline["cases"] if case["id"] == scenario)
        for prefix, arm in (("C", "baseline"), ("T", "all-relevant")):
            case = copy.deepcopy(source)
            case.update(
                id=f"{prefix}{scenario[1:]}",
                scenario=scenario,
                arm=arm,
                task=matrix["tasks"][scenario],
            )
            cases.append(case)
    return {**baseline, "schema_version": 2, "comparison": matrix, "cases": cases}


def ordered_cases(matrix: dict, repeat: int) -> list:
    cases = matrix["cases"]
    return list(reversed(cases)) if matrix["schema_version"] == 2 and repeat % 2 == 0 else cases


def validate_matrix(matrix: dict, binding: dict, root: Path) -> None:
    schema = matrix["schema_version"]
    if schema not in {1, 2} or matrix["role"] != "coder":
        raise FactoryError("Only schema-1/2 coder evaluations are supported")
    count = 6 if schema == 1 else 4
    if matrix["repetitions"] != 3 or len(matrix["cases"]) != count:
        raise FactoryError("Expected the bounded baseline or 12-session comparison")
    if schema == 2:
        for scenario in ("S3", "S4"):
            paired = [case for case in matrix["cases"] if case.get("scenario") == scenario]
            if len(paired) != 2 or {case.get("arm") for case in paired} != {
                "baseline",
                "all-relevant",
            }:
                raise FactoryError("Comparison needs two matched arms for each scenario")
            visible = [
                {key: value for key, value in case.items() if key not in {"id", "arm"}}
                for case in paired
            ]
            if visible[0] != visible[1]:
                raise FactoryError("Comparison arms differ beyond the instruction treatment")
    names = set(binding["roles"]["coder"]["skills"])
    ids = set()
    for case in matrix["cases"]:
        if not NAME.fullmatch(case["id"].lower()) or case["id"] in ids:
            raise FactoryError("Case IDs must be unique simple names")
        ids.add(case["id"])
        expected, optional = set(case["expected"]), set(case["optional"])
        if expected & optional or not (expected | optional) <= names:
            raise FactoryError("Expected/optional skills must be disjoint and in the release")
        if not case["task"].strip() or not case["obligations"]:
            raise FactoryError("Every case needs a task and review obligations")
        if any(name in case["task"] for name in names):
            raise FactoryError("Task must not reveal expected skill identifiers")
        for item in case["resources"]:
            if item["skill"] not in expected:
                raise FactoryError("Required resources must belong to expected skills")
            relative_path(item["path"])
        for path in case["proposal_paths"]:
            relative_path(path)
        if case["proposal_required"] and not case["proposal_paths"]:
            raise FactoryError("Proposal-required cases need allowed proposal paths")
        if not file_hashes(checked_fixture(root, case["fixture"])):
            raise FactoryError("Empty evaluation fixture")


def prepare_evaluation(
    matrix_path: Path,
    binding_path: Path,
    evaluations_path: Path,
    root: Path,
    output: Path,
    model: str,
    identity: dict | None = None,
) -> Path:
    matrix, binding = matrix_for(matrix_path), load(binding_path)
    release = matrix["release"]
    if digest(binding_path.read_bytes()) != release["binding_sha256"]:
        raise FactoryError("Release binding SHA-256 mismatch")
    if binding["release"]["source_commit"] != release["source_commit"]:
        raise FactoryError("Release source commit mismatch")
    if digest(evaluations_path.read_bytes()) != matrix["evaluation_sha256"]:
        raise FactoryError("Canonical evaluation definitions SHA-256 mismatch")
    if not re.fullmatch(r"[a-f0-9]{40}", release["source_commit"]) or not model.strip():
        raise FactoryError("An exact source commit and explicit model are required")
    author_evaluations = load(evaluations_path)
    if not isinstance(author_evaluations, list):
        raise FactoryError("Expected the pinned skills evaluation definitions")
    validate_matrix(matrix, binding, root)
    coder = binding["roles"]["coder"]
    pins = binding["release"]["skills"]
    for name, version in coder["skills"].items():
        if pins[name]["foundry_version"] != version or not pins[name]["files"]:
            raise FactoryError("Release content hashes and Toolbox pins disagree")
        for path, value in pins[name]["files"].items():
            relative_path(path)
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                raise FactoryError("Release contains an invalid file hash")
    if not set(coder["skills"]) <= {row["skill"] for row in author_evaluations}:
        raise FactoryError("Missing canonical evaluation definitions")
    directory = output.resolve() / str(uuid.uuid4())
    consumer = {key: coder[key] for key in ("project_endpoint", "toolbox", "version")}
    if identity is not None:
        required = {"tenant_id", "client_id", "principal_id"}
        if not required <= identity.keys():
            raise FactoryError("Consumer identity needs tenant, client and principal IDs")
        consumer["identity"] = identity
    fixtures = {}
    for case in matrix["cases"]:
        target = checked_fixture(root, case["fixture"])
        if directory.is_relative_to(target.resolve()):
            raise FactoryError("Evaluation output cannot be inside a fixture")
        fixtures[case["id"]] = {
            "files": file_hashes(target),
            "profile": profile(repository_files(target)),
        }
    revision = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    # Include uncommitted consumer code, without recording ignored credentials/artifacts.
    source_files = consumer_hashes(root)
    configuration = {
        "schema_version": 1,
        "evaluation_id": directory.name,
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(root.resolve()),
        "factory_revision": revision,
        "factory_dirty": bool(dirty),
        "factory_files": source_files,
        "factory_content_sha256": digest(canonical(source_files)),
        "matrix": matrix,
        "matrix_sha256": digest(canonical(matrix)),
        "release": release,
        "release_skill_records": pins,
        "consumer_binding": consumer,
        "skill_versions": coder["skills"],
        "credential_method": "AzureCliCredential",
        "model": model,
        "sdk_version": SDK_VERSION,
        "cli_version": CLI_VERSION,
        "skill_selection_instructions": SKILL_SELECTION_INSTRUCTIONS,
        "schedule": [
            {"case": case["id"], "repeat": repeat}
            for repeat in range(1, matrix["repetitions"] + 1)
            for case in ordered_cases(matrix, repeat)
        ],
        "policy": {
            "allowed_skills": sorted(coder["skills"]),
            "permitted_skills": {"coder": sorted(coder["skills"])},
        },
        "fixtures": fixtures,
        "canonical_evaluations": author_evaluations,
        "canonical_evaluations_sha256": digest(evaluations_path.read_bytes()),
        "review_status": "Consumer rubric requires approval before live runs",
    }
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "configuration.json", configuration)
    (directory / "configuration.sha256").write_text(
        digest(canonical(configuration)) + "\n", encoding="ascii"
    )
    write_json(directory / "results.json", [])
    write_json(directory / "reviews.json", [])
    render_report(directory)
    return directory


def configuration_for(directory: Path) -> dict:
    config = load(directory / "configuration.json")
    expected = (directory / "configuration.sha256").read_text().strip()
    if digest(canonical(config)) != expected:
        raise FactoryError("Evaluation configuration changed")
    return config


def fetch_inputs(matrix_path: Path, output: Path) -> None:
    matrix = matrix_for(matrix_path)
    release = matrix["release"]
    repository = release["repository"]
    metadata = json.loads(
        subprocess.run(
            ["gh", "api", f"repos/{repository}/releases/tags/{release['tag']}"],
            capture_output=True,
            check=True,
        ).stdout
    )
    assets = [asset for asset in metadata["assets"] if asset["name"] == "role-bindings.json"]
    if len(assets) != 1:
        raise FactoryError("Release must contain exactly one role-bindings.json asset")
    binding = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repository}/releases/assets/{assets[0]['id']}",
            "-H",
            "Accept: application/octet-stream",
        ],
        capture_output=True,
        check=True,
    ).stdout
    definition = json.loads(
        subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repository}/contents/{matrix['evaluation_source']}"
                f"?ref={release['source_commit']}",
            ],
            capture_output=True,
            check=True,
        ).stdout
    )
    evaluations = base64.b64decode(definition["content"])
    if (
        digest(binding) != release["binding_sha256"]
        or digest(evaluations) != matrix["evaluation_sha256"]
    ):
        raise FactoryError("Downloaded release/evaluation hashes differ from the matrix")
    output.mkdir(parents=True, exist_ok=False)
    (output / "role-bindings.json").write_bytes(binding)
    (output / "evaluations.json").write_bytes(evaluations)


class ObservedClient(CopilotClient):
    def __init__(self, evidence: Path, **options):
        self.evidence = evidence
        super().__init__(**options)

    async def create_session(self, **configuration):
        original = configuration["on_event"]

        def observed(notification):
            if getattr(notification.type, "value", "") == "assistant.usage":
                with (self.evidence / "usage.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(notification.to_dict()) + "\n")
            original(notification)

        configuration["on_event"] = observed
        session = await super().create_session(**configuration)
        evidence = self.evidence

        class ObservedSession:
            def __getattr__(self, name):
                return getattr(session, name)

            async def send_and_wait(self, *args, **kwargs):
                response = await session.send_and_wait(*args, **kwargs)
                write_json(
                    evidence / "evaluation-response.json",
                    {"content": getattr(getattr(response, "data", None), "content", None)},
                )
                return response

        return ObservedSession()


def local_client(binding):
    from azure.identity.aio import AzureCliCredential

    return ToolboxClient(binding, credential=AzureCliCredential())


async def preflight_runtime(directory: Path, config: dict, client_factory) -> None:
    working = directory / "preflight"
    working.mkdir(exist_ok=True)
    client = client_factory(
        directory,
        working_directory=str(working),
        base_directory=str(working / "sdk"),
        builtin_plugin_directories=[],
        env=_runtime_environment(),
        github_token=os.environ.get("FACTORY_COPILOT_TOKEN"),
    )
    try:
        async with asyncio.timeout(60):
            await client.start()
            if not (await client.get_auth_status()).isAuthenticated:
                raise FactoryError("Copilot is not authenticated")
            if (await client.get_status()).version != config["cli_version"]:
                raise FactoryError("Actual CLI differs from the evaluation pin")
            if config["model"] not in {model.id for model in await client.list_models()}:
                raise FactoryError("Explicit evaluation model is unavailable")
    finally:
        await client.stop()


def classify_error(error: Exception) -> str:
    message = str(error).lower()
    if isinstance(error, TimeoutError) or any(
        word in message for word in ("deadline", "timed out", "timeouterror")
    ):
        return "timeout"
    if any(word in message for word in ("http 401", "http 403", "authenticat", "credential")):
        return "authentication"
    if any(word in message for word in ("transport", "http 429", "http 50")):
        return "transport"
    if "no native skill activation" in message:
        return "mandatory-activation-policy"
    return "runtime-or-policy"


def evidence_hash(evidence: Path) -> str:
    hashes = {
        path.relative_to(evidence).as_posix(): digest(path.read_bytes())
        for path in sorted(evidence.rglob("*"))
        if path.is_file() and "runtime" not in path.relative_to(evidence).parts
    }
    return digest(canonical(hashes))


def score_attempt(directory: Path, attempt: dict, config: dict, case: dict) -> dict:
    evidence = directory / attempt["evidence"]
    manifest_path = evidence / "manifest.json"
    metrics_path = evidence / "coder.provider-metrics.json"
    native_path = evidence / "native-skill-events.jsonl"
    events_path = evidence / "events.jsonl"
    manifest = load(manifest_path) if manifest_path.exists() else {}
    metrics = load(metrics_path) if metrics_path.exists() else {}
    native = (
        [json.loads(line) for line in native_path.read_text(encoding="utf-8").splitlines()]
        if native_path.exists()
        else []
    )
    events = (
        [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        if events_path.exists()
        else []
    )
    activated = {item["data"]["name"] for item in native}
    expected, optional = set(case["expected"]), set(case["optional"])
    permitted = set(config["policy"]["permitted_skills"]["coder"])
    resource_reads = {
        (item["skill"], item["path"]) for item in events if item["kind"] == "skill.resource_read"
    }
    missing_resources = [
        item for item in case["resources"] if (item["skill"], item["path"]) not in resource_reads
    ]
    proposal_files = sorted((evidence / "proposals").glob("*.json"))
    proposals = [load(path) for path in proposal_files]
    verified = False
    if manifest:
        try:
            verify(evidence)
            verified = manifest["target"]["files"] == config["fixtures"][case["id"]]["files"]
        except (FactoryError, OSError, KeyError, ValueError):
            pass
    published = config["release_skill_records"]
    materialized = set(metrics.get("materialized", []))
    guardrails = {
        "snapshot_integrity": verified,
        "input_unchanged": file_hashes(checked_fixture(Path(config["root"]), case["fixture"]))
        == config["fixtures"][case["id"]]["files"],
        "role_and_policy": manifest.get("roles") == ["coder"]
        and manifest.get("policy") == config["policy"],
        "eligible_activations_only": activated <= permitted,
        "native_provider_events": all(
            item["data"].get("source") == "sdk"
            and item.get("type") in {"skill.invoked", "skill.invoked.ref"}
            for item in native
        ),
        "activation_materialized": activated <= materialized,
        "pinned_bytes": bool(manifest)
        and materialized <= permitted
        and all(
            manifest["skills"][name].get("files") == published[name]["files"]
            for name in materialized
        ),
        "temporary_directory_removed": metrics.get("temporary_directory_removed") is True,
        "provider_succeeded": bool(metrics) and not metrics.get("failure"),
        "runtime_pins": manifest.get("runtime", {}).get("version") == config["sdk_version"]
        and manifest.get("runtime", {}).get("cli_version") == config["cli_version"]
        and manifest.get("distribution", {}).get("skill_versions") == config["skill_versions"]
        and manifest.get("runtime", {}).get("skill_selection", "baseline")
        == case.get("arm", "baseline"),
        "proposal_integrity": all(
            item.get("status") == "approval-required"
            and item.get("path") in case["proposal_paths"]
            and item.get("content_sha256") == digest(item.get("content", "").encode())
            and item.get("base_sha256")
            == config["fixtures"][case["id"]]["files"].get(item.get("path"))
            for item in proposals
        ),
    }
    checks = {
        "runtime_completed": attempt["status"] == "completed",
        "native_activation_observed": bool(native),
        "expected_activations": expected <= activated,
        "no_irrelevant_activations": activated <= expected | optional,
        "required_resource_reads": not missing_resources,
        "proposal_present_if_required": bool(proposals) or not case["proposal_required"],
    }
    scored = attempt["status"] == "completed"
    usage_path = evidence / "usage.jsonl"
    return {
        **attempt,
        "discovered": sorted(load(directory / "discovery.json")),
        "eligible": sorted(permitted),
        "fetched_uris": sorted({row["uri"] for row in metrics.get("calls", []) if row.get("uri")}),
        "materialized": sorted(materialized),
        "activated": sorted(activated),
        "missing": sorted(expected - activated),
        "unnecessary": sorted(activated - expected - optional),
        "expected_hits": len(expected & activated),
        "expected_count": len(expected),
        "resource_reads": [
            {"skill": skill, "path": path} for skill, path in sorted(resource_reads)
        ],
        "missing_resources": missing_resources,
        "proposals": [item["path"] for item in proposals],
        "guardrails": guardrails,
        "checks": checks,
        "deterministic_pass": all(checks.values()) and all(guardrails.values()),
        "selection_scored": scored,
        "guidance_use": "uncertain",
        "http_bytes": metrics.get("bytes", 0),
        "usage_events": len(usage_path.read_text(encoding="utf-8").splitlines())
        if usage_path.exists()
        else 0,
        "evidence_sha256": evidence_hash(evidence),
    }


def execute_evaluation(
    directory: Path,
    *,
    live: bool,
    approve_rubric: bool,
    client_factory=local_client,
    runner=run,
    observed_factory=ObservedClient,
) -> None:
    if not live or not approve_rubric:
        raise FactoryError("Both --live and --approve-rubric are required")
    config = configuration_for(directory)
    root = Path(config["root"])
    if config["sdk_version"] != SDK_VERSION or config["cli_version"] != CLI_VERSION:
        raise FactoryError("Evaluation requires a different runtime")
    if config.get("skill_selection_instructions", SKILL_SELECTION_INSTRUCTIONS) != (
        SKILL_SELECTION_INSTRUCTIONS
    ):
        raise FactoryError("Skill-selection instruction text changed after preparation")
    for case in config["matrix"]["cases"]:
        if (
            file_hashes(checked_fixture(root, case["fixture"]))
            != config["fixtures"][case["id"]]["files"]
        ):
            raise FactoryError("Fixture changed after evaluation preparation")
    if consumer_hashes(root) != config["factory_files"]:
        raise FactoryError("Consumer source changed; prepare a new evaluation")
    lock = directory / ".evaluation.lock"
    try:
        lock.touch(exist_ok=False)
    except FileExistsError:
        raise FactoryError("Evaluation already active; inspect its lock before retrying") from None
    try:
        results = load(directory / "results.json")
        if results:
            raise FactoryError("This evaluation already has attempts; prepare a fresh evaluation")
        try:
            snapshot = ToolboxProviderResolver(
                config["consumer_binding"], client_factory=client_factory
            ).resolve()
            if snapshot.source["skill_versions"] != config["skill_versions"]:
                raise FactoryError("Discovered Toolbox references differ from the release")
            if "identity" in config["consumer_binding"]:
                snapshot.source["identity"] = config["consumer_binding"]["identity"]
            write_json(directory / "discovery.json", snapshot.records)
            asyncio.run(preflight_runtime(directory, config, observed_factory))
        except Exception as exc:
            write_json(
                directory / "preflight.json",
                {
                    "status": "failed",
                    "failure_class": classify_error(exc),
                    "error_type": type(exc).__name__,
                },
            )
            raise FactoryError("Preflight failed; see preflight.json (no model attempts)") from None
        write_json(directory / "preflight.json", {"status": "passed"})
        write_json(
            directory / "approval.json",
            {
                "configuration_sha256": digest(canonical(config)),
                "rubric_approved": True,
                "live_authorized": True,
                "at": datetime.now(UTC).isoformat(),
            },
        )
        for repeat in range(1, config["matrix"]["repetitions"] + 1):
            for case in ordered_cases(config["matrix"], repeat):
                started = time.perf_counter()
                execution = prepare(
                    checked_fixture(root, case["fixture"]),
                    copy.deepcopy(snapshot),
                    directory / "runs" / case["id"] / str(repeat),
                    {"id": case.get("scenario", case["id"]), "description": case["task"]},
                    config["policy"],
                    ["coder"],
                    ["implementation"],
                )
                manifest = load(execution / "manifest.json")
                if config["matrix"]["schema_version"] == 2:
                    manifest["runtime"]["skill_selection"] = case["arm"]
                for name, record in manifest["skills"].items():
                    hashes = config["release_skill_records"][name]["files"]
                    record.update(files=hashes, sha256=digest(canonical(hashes)))
                write_json(execution / "manifest.json", manifest, atomic=True)
                verify(execution)
                attempt = {
                    "case": case["id"],
                    "repeat": repeat,
                    "status": "started",
                    "evidence": execution.relative_to(directory).as_posix(),
                }
                if config["matrix"]["schema_version"] == 2:
                    attempt.update(
                        scenario=case["scenario"], arm=case["arm"], order=len(results) + 1
                    )
                results.append(attempt)
                write_json(directory / "results.json", results, atomic=True)
                try:
                    asyncio.run(
                        runner(
                            execution,
                            config["model"],
                            roles=["coder"],
                            timeout=180,
                            skill_selection=case.get("arm", "baseline"),
                            client_factory=partial(observed_factory, execution),
                            provider_factory=partial(
                                ToolboxSkillProvider, client_factory=client_factory
                            ),
                        )
                    )
                    attempt["status"] = "completed"
                except Exception as exc:
                    attempt.update(
                        status="failed",
                        failure_class=classify_error(exc),
                        error_type=type(exc).__name__,
                    )
                finally:
                    attempt["seconds"] = round(time.perf_counter() - started, 3)
                    results[-1] = score_attempt(directory, attempt, config, case)
                    write_json(directory / "results.json", results, atomic=True)
                    render_report(directory)
                    print(
                        json.dumps(
                            {
                                "case": case["id"],
                                "repeat": repeat,
                                "status": results[-1]["status"],
                                "activated": results[-1]["activated"],
                                "deterministic_pass": results[-1]["deterministic_pass"],
                            }
                        ),
                        flush=True,
                    )
    finally:
        lock.unlink()


def reviewed_results(directory: Path, results: list, reviews: list) -> list:
    indexed = {}
    for review in reviews:
        key = review["case"], review["repeat"]
        if key in indexed or review["status"] not in {"pass", "fail", "uncertain"}:
            raise FactoryError("Duplicate or invalid semantic review")
        if key not in {(row["case"], row["repeat"]) for row in results}:
            raise FactoryError("Review does not refer to an attempted run")
        indexed[key] = review
    output = []
    for row in results:
        if evidence_hash(directory / row["evidence"]) != row["evidence_sha256"]:
            raise FactoryError("Attempt artifacts changed; evidence and reviews are stale")
        result = dict(row)
        if row.get("status") == "failed" and row.get("failure_class") == "runtime-or-policy":
            metrics_path = directory / row["evidence"] / "coder.provider-metrics.json"
            if metrics_path.exists():
                classified = classify_error(FactoryError(load(metrics_path).get("failure") or ""))
                if classified != "runtime-or-policy":
                    result["recorded_failure_class"] = row["failure_class"]
                    result["failure_class"] = classified
        review = indexed.get((row["case"], row["repeat"]))
        result["guidance_use"] = "uncertain"
        if review and (
            review["status"] != "uncertain"
            or review.get("notes")
            or review.get("evidence")
            or review.get("reviewer")
        ):
            if review["evidence_sha256"] != row["evidence_sha256"]:
                raise FactoryError("Semantic review refers to stale evidence")
            if not review.get("reviewer") or not review.get("notes") or not review.get("evidence"):
                raise FactoryError("Semantic review needs reviewer, notes and evidence references")
            evidence_root = directory / row["evidence"]
            for name in review["evidence"]:
                relative_path(name)
                path = evidence_root / name
                if not path.resolve().is_relative_to(evidence_root.resolve()) or not path.is_file():
                    raise FactoryError("Semantic review references missing or external evidence")
            result["guidance_use"] = review["status"]
            result["review_notes"] = review["notes"]
            result["review_evidence"] = review["evidence"]
        result["case_pass"] = result["deterministic_pass"] and result["guidance_use"] == "pass"
        output.append(result)
    return output


def comparison_counts(config: dict, results: list) -> list:
    if config["matrix"]["schema_version"] != 2:
        return []
    output = []
    for arm in ("baseline", "all-relevant"):
        ids = {case["id"] for case in config["matrix"]["cases"] if case["arm"] == arm}
        rows = [row for row in results if row["case"] in ids]
        scored = [row for row in rows if row["selection_scored"]]
        output.append(
            {
                "arm": arm,
                "attempted": len(rows),
                "completed": len(scored),
                "expected_hits": sum(row["expected_hits"] for row in scored),
                "expected_total": sum(row["expected_count"] for row in scored),
                "complete_relevant_sets": sum(
                    row["checks"]["expected_activations"]
                    and row["checks"]["no_irrelevant_activations"]
                    for row in scored
                ),
                "unnecessary": sum(len(row["unnecessary"]) for row in scored),
                "resources_satisfied": sum(
                    row["checks"]["required_resource_reads"] for row in scored
                ),
                "guidance_pass": sum(row["guidance_use"] == "pass" for row in scored),
                "case_pass": sum(row["case_pass"] for row in rows),
            }
        )
    return output


def render_report(directory: Path) -> None:
    config = configuration_for(directory)
    attempts = load(directory / "results.json")
    reviews = load(directory / "reviews.json")
    present = {(row["case"], row["repeat"]) for row in reviews}
    for row in attempts:
        if (row["case"], row["repeat"]) not in present:
            reviews.append(
                {
                    "case": row["case"],
                    "repeat": row["repeat"],
                    "evidence_sha256": row["evidence_sha256"],
                    "status": "uncertain",
                    "reviewer": "",
                    "notes": "",
                    "evidence": [],
                }
            )
    write_json(directory / "reviews.json", reviews)
    results = reviewed_results(directory, attempts, reviews)
    planned = len(config["matrix"]["cases"]) * config["matrix"]["repetitions"]
    passed = sum(row["case_pass"] for row in results)
    reviewed = sum(row["guidance_use"] != "uncertain" for row in results)
    completed = [row for row in results if row["status"] == "completed"]
    accounted = sum(row["status"] in {"completed", "failed"} for row in results)
    output_reviews_complete = all(row["guidance_use"] != "uncertain" for row in completed)
    recommendation = "Collect more evidence; evaluation or semantic review is incomplete."
    if accounted == planned and output_reviews_complete:
        if passed == planned:
            recommendation = "Keep native selection for this evaluated scope."
        elif any(not row["case_pass"] for row in completed):
            recommendation = (
                "Investigate the recorded failures before proposing a narrower comparison."
            )
        else:
            recommendation = "Collect more evidence; infrastructure failures left unscored cases."
    comparison = comparison_counts(config, results)
    if comparison and accounted == planned and output_reviews_complete:
        recommendation = (
            "Compare activation, resource use and output judgments separately; "
            "do not infer reliability or add a selector from this small comparison alone."
        )
    lines = [
        "# Skill Selection Validation Report",
        "",
        f"Evaluation: `{config['evaluation_id']}`",
        f"Release: `{config['release']['tag']}`; model: `{config['model']}`.",
        f"SDK: `{config['sdk_version']}`; CLI: `{config['cli_version']}`.",
        f"Attempted: {len(results)}/{planned}; accounted: {accounted}; "
        f"completed: {len(completed)}; output-reviewed: {reviewed}; passed: {passed}.",
        "",
        f"**Recommendation:** {recommendation}",
        "",
        "| Case | Repeat | Runtime / failure | Missing | Unnecessary | Checks | Guidance |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in results:
        lines.append(
            f"| {row['case']} | {row['repeat']} "
            f"| {row['status']} / {row.get('failure_class', '-')} "
            f"| {(', '.join(row['missing']) or '-') if row['selection_scored'] else 'not scored'} "
            f"| {len(row['unnecessary']) if row['selection_scored'] else 'not scored'} "
            f"| {'pass' if row['deterministic_pass'] else 'fail'} | {row['guidance_use']} |"
        )
    lines += ["", "## Counts by scenario", ""]
    lines += [
        "| Case | Attempts | Completed | Expected hits | Unnecessary | Passed |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in config["matrix"]["cases"]:
        rows = [row for row in results if row["case"] == case["id"]]
        scored = [row for row in rows if row["selection_scored"]]
        hits = sum(row["expected_hits"] for row in scored)
        total = sum(row["expected_count"] for row in scored)
        lines.append(
            f"| {case['id']} | {len(rows)}/3 "
            f"| {sum(row['status'] == 'completed' for row in rows)} "
            f"| {hits}/{total} | {sum(len(row['unnecessary']) for row in scored)} "
            f"| {sum(row['case_pass'] for row in rows)}/3 |"
        )
    if comparison:
        lines += [
            "",
            "## Instruction-only comparison",
            "",
            "Two matched scenarios, three fresh sessions per arm and scenario. "
            "Pair order is reversed in repetition 2; no expected skill names enter task prompts.",
            "Primary measure: complete expected skill set without irrelevant activation. "
            "Output correctness and resource reads remain separate measures.",
            "",
            "| Arm | Completed / attempted | Expected hits | Complete sets | Unnecessary "
            "| Resources | Guidance passes | Full passes |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in comparison:
            lines.append(
                f"| {row['arm']} | {row['completed']}/{row['attempted']} "
                f"| {row['expected_hits']}/{row['expected_total']} "
                f"| {row['complete_relevant_sets']} | {row['unnecessary']} "
                f"| {row['resources_satisfied']} | {row['guidance_pass']} | {row['case_pass']} |"
            )
        for arm, instruction in config["skill_selection_instructions"].items():
            lines += ["", f"**{arm}:** {instruction.strip()}"]
    lines += ["", "## Attempt evidence", ""]
    for row in results:
        evidence = row["evidence"]
        guardrail_failures = ", ".join(
            key for key, passed in row["guardrails"].items() if not passed
        )
        lines += [
            f"### {row['case']} / {row['repeat']}",
            "",
            f"[Artifacts]({evidence}/), [manifest]({evidence}/manifest.json); "
            f"evidence SHA-256: `{row['evidence_sha256']}`.",
            f"Activated: {', '.join(row['activated']) or 'none'}; "
            f"materialized: {', '.join(row['materialized']) or 'none'}.",
            f"Guardrail failures: {guardrail_failures or 'none'}.",
            f"Elapsed: {row['seconds']} s; HTTP bytes: {row['http_bytes']}; "
            f"usage events retained: {row['usage_events']}.",
            f"Guidance review: {row.get('review_notes', 'Pending evidence-linked review.')}",
            *(
                [
                    f"Failure classification normalized from "
                    f"`{row['recorded_failure_class']}` to `{row['failure_class']}` "
                    "using provider failure evidence; results.json retains the recorded value."
                ]
                if "recorded_failure_class" in row
                else []
            ),
            *[
                f"- [Reviewed artifact]({evidence}/{name})"
                for name in row.get("review_evidence", [])
            ],
            "",
        ]
    lines += ["", "## Expected behavior and guidance review", ""]
    for case in config["matrix"]["cases"]:
        lines += [
            f"### {case['id']}",
            "",
            f"Expected: {', '.join(case['expected']) or 'no applicable skill'}.",
            f"Optional: {', '.join(case['optional']) or 'none'}.",
            *[f"- {item}" for item in case["obligations"]],
            "",
        ]
    lines += [
        "## Limits",
        "",
        "Three repetitions are demonstration evidence, not a reliability estimate.",
        "Resource reads and activation do not by themselves prove guidance use.",
        "No-skill success remains unsupported by the unchanged mandatory-activation policy. "
        "Only the original baseline includes the S6 negative control.",
        "Authentication/transport/timeouts are recorded separately and are not selection scores.",
        "Application compilation/tests were NOT RUN; proposals were not applied.",
        "Proposal integrity includes the rubric's output paths, which the worker does not "
        "enforce as a per-case authorization rule. Extra proposal paths are output-contract "
        "mismatches, not evidence that the worker modified unauthorized source files.",
        "Semantic judgments are reviewer opinions, not execution evidence or human acceptance.",
        "No Tool Search ranking, toolbox authorization isolation or runtime portability is proven.",
        "Usage events are retained when emitted; no monetary cost is inferred.",
        "Raw artifacts contain environment metadata; do not publish them automatically.",
        "",
    ]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    summary = {
        "evaluation_id": config["evaluation_id"],
        "release": config["release"]["tag"],
        "model": config["model"],
        "planned": planned,
        "attempted": len(results),
        "accounted": accounted,
        "completed": len(completed),
        "output_reviews_complete": output_reviews_complete,
        "reviewed": reviewed,
        "passed": passed,
        "recommendation": recommendation,
        "comparison": comparison,
        "cases": [
            {
                "failure_class": row.get("failure_class"),
                "recorded_failure_class": row.get("recorded_failure_class"),
                **{
                    key: row[key]
                    for key in (
                        "case",
                        "repeat",
                        "status",
                        "missing",
                        "unnecessary",
                        "guardrails",
                        "guidance_use",
                        "case_pass",
                        "selection_scored",
                    )
                },
            }
            for row in results
        ],
    }
    write_json(directory / "summary.json", summary)


def main(root: Path) -> None:
    parser = argparse.ArgumentParser(description="Bounded local native skill selection evaluation")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("--matrix", type=Path, default=root / "examples/skill-selection-matrix.json")
    fetch.add_argument("--output", type=Path, required=True)
    init = commands.add_parser("prepare")
    init.add_argument("--matrix", type=Path, default=root / "examples/skill-selection-matrix.json")
    init.add_argument("--binding", type=Path, required=True)
    init.add_argument("--evaluations", type=Path, required=True)
    init.add_argument(
        "--identity", type=Path, help="Local consumer identity; ignores release identity"
    )
    init.add_argument("--model", required=True)
    init.add_argument("--output", type=Path, default=root / ".artifacts/skill-selection")
    live = commands.add_parser("run")
    live.add_argument("evaluation", type=Path)
    live.add_argument("--live", action="store_true", required=True)
    live.add_argument("--approve-rubric", action="store_true", required=True)
    report = commands.add_parser("report")
    report.add_argument("evaluation", type=Path)
    args = parser.parse_args()
    if args.command == "fetch":
        fetch_inputs(args.matrix, args.output)
    elif args.command == "prepare":
        print(
            prepare_evaluation(
                args.matrix,
                args.binding,
                args.evaluations,
                root,
                args.output,
                args.model,
                load(args.identity) if args.identity else None,
            )
        )
    elif args.command == "run":
        execute_evaluation(
            args.evaluation.resolve(), live=args.live, approve_rubric=args.approve_rubric
        )
    else:
        render_report(args.evaluation.resolve())
