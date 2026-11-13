import json
from types import SimpleNamespace

import pytest

from ai_factory.contracts import CLI_VERSION, FactoryError
from ai_factory.execution import prepare, verify
from ai_factory.runtime import WorkerTools, run


@pytest.fixture
def execution(target, snapshot, policy, request_data, tmp_path):
    return prepare(
        target,
        snapshot,
        tmp_path / "runs",
        request_data,
        policy,
        ["coder", "tester"],
        ["implementation"],
    )


def test_tools_enforce_snapshot_and_role_boundaries(execution):
    manifest = verify(execution)
    coder = WorkerTools(execution, manifest, "coder")
    before = (execution / "target/OrderService.cs").read_bytes()
    content = before.decode().replace(
        "return reference.Trim()",
        "ArgumentException.ThrowIfNullOrWhiteSpace(reference);\n        return reference.Trim()",
    )
    coder.propose("OrderService.cs", content, manifest["target"]["files"]["OrderService.cs"])
    assert (execution / "target/OrderService.cs").read_bytes() == before
    proposal = json.loads(coder.proposals())[0]
    assert proposal["status"] == "approval-required" and proposal["patch"]
    with pytest.raises(FactoryError):
        coder.read_project("../manifest.json")
    with pytest.raises(FactoryError):
        coder.read_project("not-in-snapshot.cs")
    with pytest.raises(FactoryError):
        coder.read_resource("java-development", "SKILL.md")
    with pytest.raises(FactoryError):
        coder.propose("OrderService.cs", content, "incorrect-base")
    tester = WorkerTools(execution, manifest, "tester")
    with pytest.raises(FactoryError):
        tester.propose("new.cs", "code", None)
    assert "propose_file" not in {tool.name for tool in tester.definitions()}
    assert coder.read_resource("dotnet-development", "references/guide.md")


@pytest.mark.parametrize("kind", ["shell", "write", "url", "mcp", "hook", "custom-tool"])
def test_native_permissions_deny_unapproved_capabilities(execution, kind):
    tools = WorkerTools(execution, verify(execution), "coder")
    request = SimpleNamespace(kind=kind, tool_name="commit_and_push")
    assert tools.permission(request, {}).kind == "denied-by-rules"


class FakeClient:
    activate = True
    observed = []
    version = CLI_VERSION

    def __init__(self, **options):
        self.options = options
        self.stopped = False

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True

    async def get_auth_status(self):
        return SimpleNamespace(isAuthenticated=True)

    async def get_status(self):
        return SimpleNamespace(version=self.version)

    async def create_session(self, **config):
        self.observed.append(config)
        client = self

        class FakeSession:
            async def send_and_wait(self, prompt, timeout):
                if client.activate:
                    config["on_event"](
                        SimpleNamespace(
                            type=SimpleNamespace(value="skill.invoked"),
                            data=SimpleNamespace(name="dotnet-development"),
                        )
                    )
                return SimpleNamespace(data=SimpleNamespace(content="Evidence and NOT RUN."))

            async def disconnect(self):
                pass

        return FakeSession()


async def test_runtime_config_and_native_activation_evidence(execution):
    FakeClient.observed = []
    results = await run(execution, "test-model", client_factory=FakeClient)
    assert set(results) == {"coder", "tester"}
    for config in FakeClient.observed:
        assert config["available_tools"] == [
            "skill",
            *sorted(tool.name for tool in config["tools"]),
        ]
        assert config["enable_config_discovery"] is False
        assert config["enable_file_hooks"] is False
        assert config["enable_host_git_operations"] is False
        assert config["included_builtin_skills"] == []
        assert config["mcp_servers"] == {}
    events = [json.loads(line) for line in (execution / "events.jsonl").read_text().splitlines()]
    assert len([item for item in events if item["kind"] == "skill.activated"]) == 2
    # Resume reuses persisted results, without another model session.
    count = len(FakeClient.observed)
    assert await run(execution, "test-model", client_factory=FakeClient) == results
    assert len(FakeClient.observed) == count


async def test_claimed_skill_use_without_native_event_is_not_success(execution):
    class NoSkillClient(FakeClient):
        activate = False

    with pytest.raises(FactoryError, match="No native"):
        await run(execution, "test-model", roles=["coder"], client_factory=NoSkillClient)
    assert not (execution / "coder.result.json").exists()


async def test_runtime_version_mismatch_is_rejected(execution):
    class WrongVersionClient(FakeClient):
        version = "0.0.0"

    with pytest.raises(FactoryError, match="CLI runtime"):
        await run(execution, "test-model", client_factory=WrongVersionClient)


@pytest.mark.parametrize("operation", ["info", "run"])
async def test_runtime_ignores_inherited_cli_path(execution, monkeypatch, operation):
    from ai_factory import runtime

    monkeypatch.setenv("COPILOT_CLI_PATH", "unapproved-runtime.exe")
    monkeypatch.setenv("FACTORY_RUNTIME_TEST", "preserved")

    class PinnedClient(FakeClient):
        def __init__(self, **options):
            super().__init__(**options)
            assert "COPILOT_CLI_PATH" not in options["env"]
            assert options["env"]["FACTORY_RUNTIME_TEST"] == "preserved"

    if operation == "info":
        monkeypatch.setattr(runtime, "CopilotClient", PinnedClient)
        assert (await runtime.runtime_info())["cli_version"] == CLI_VERSION
    else:
        await run(execution, "test-model", client_factory=PinnedClient)
    assert runtime.os.environ["COPILOT_CLI_PATH"] == "unapproved-runtime.exe"


async def test_execution_cannot_change_model_or_run_concurrently(execution):
    await run(execution, "test-model", client_factory=FakeClient)
    with pytest.raises(FactoryError, match="cannot change"):
        await run(execution, "other-model", client_factory=FakeClient)
    (execution / ".worker.lock").write_text("existing-worker")
    with pytest.raises(FactoryError, match="already has a worker"):
        await run(execution, "test-model", client_factory=FakeClient)


def test_worker_tools_require_actual_native_activation(execution):
    tools = WorkerTools(execution, verify(execution), "coder", activated=[])
    definition = next(t for t in tools.definitions() if t.name == "read_project_file")
    result = definition.handler(SimpleNamespace(arguments={"path": "OrderService.cs"}))
    assert result.result_type == "failure"
    assert "native skill" in result.text_result_for_llm


def test_job_resume_needs_no_current_distribution_configuration(execution, monkeypatch, capsys):
    from ai_factory.job import main

    monkeypatch.setenv("FACTORY_ARTIFACT_ROOT", str(execution.parent))
    monkeypatch.setenv("FACTORY_RESUME_EXECUTION", execution.name)
    monkeypatch.delenv("FACTORY_SOURCE", raising=False)
    monkeypatch.delenv("FACTORY_LIVE", raising=False)
    main()
    assert json.loads(capsys.readouterr().out)["status"] == "prepared"
