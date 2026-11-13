import json
import os
from pathlib import Path

import pytest

from ai_factory.execution import prepare, verify
from ai_factory.resolvers import LocalResolver
from ai_factory.runtime import run

pytestmark = pytest.mark.skipif(
    os.environ.get("FACTORY_LIVE") != "1",
    reason="Opt-in live model evaluation requires FACTORY_LIVE=1 and existing authentication",
)


async def test_real_native_skills_and_resource_guided_output(target, request_data, tmp_path):
    root = Path(os.environ["FACTORY_SKILLS_ROOT"])
    snapshot = LocalResolver(root).resolve()
    policy = {
        "allowed_skills": list(snapshot.entries),
        "required_skills": {"coder": ["dotnet-development"], "tester": ["test-strategy"]},
    }
    request_data["description"] = (
        "Propose a minimal change to OrderService.NormalizeReference to reject null, empty, "
        "and whitespace references with ArgumentException while preserving valid behavior. "
        "Coder: submit the proposed file. Tester: inspect proposals and give boundary cases. "
        "No database/API changes, no commands."
    )
    execution = prepare(
        target,
        snapshot,
        tmp_path / "runs",
        request_data,
        policy,
        ["coder", "tester"],
        ["implementation", "testing", "api", "database"],
    )
    original = (execution / "target/OrderService.cs").read_bytes()
    results = await run(execution, os.environ["FACTORY_MODEL"])
    events = [json.loads(line) for line in (execution / "events.jsonl").read_text().splitlines()]
    # Native runtime events and a concrete supporting-file read establish actual loading.
    assert "dotnet-development" in results["coder"]["skills_activated"]
    assert "test-strategy" in results["tester"]["skills_activated"]
    assert any(
        item["kind"] == "skill.resource_read"
        and item["skill"] == "dotnet-development"
        and item["path"] == "references/validation.md"
        for item in events
    )
    assert all(
        item.get("skill") not in {"database-changes", "api-development"}
        for item in events
        if item["kind"] == "skill.activated"
    )
    # These details come from the supporting resource, not the change request.
    coder_output = results["coder"]["content"].lower()
    assert "compatibility evidence" in coder_output and "net8.0" in coder_output
    assert "nullable" in coder_output and "orders.csproj" in coder_output
    assert list((execution / "proposals").glob("*.json"))
    assert (execution / "target/OrderService.cs").read_bytes() == original
    assert verify(execution)["execution_id"] == execution.name
