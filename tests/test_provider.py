import asyncio
import base64
import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from test_resolvers import archive
from test_runtime import FakeClient

from ai_factory.contracts import FactoryError, canonical, digest
from ai_factory.execution import prepare, verify
from ai_factory.provider import (
    ProviderSnapshot,
    ToolboxClient,
    ToolboxProviderResolver,
    ToolboxSkillProvider,
)
from ai_factory.runtime import WorkerTools, run


class Credential:
    def __init__(self):
        self.calls = 0
        self.closed = False

    async def get_token(self, scope):
        assert scope == "https://ai.azure.com/.default"
        self.calls += 1
        return SimpleNamespace(token=f"refreshed-token-{self.calls}")

    async def close(self):
        self.closed = True


@pytest.fixture
def toolbox(snapshot):
    binding = {
        "project_endpoint": "https://test.services.ai.azure.com/api/projects/test",
        "toolbox": "factory",
        "version": "1",
    }
    state = SimpleNamespace(calls=[], outage=False, defect=None, credential=Credential())
    records = {}
    for name in snapshot.entries:
        front = yaml.safe_load(snapshot.skill_files(name)["SKILL.md"].decode().split("---", 2)[1])
        records[name] = {
            "name": name,
            "description": front["description"],
            "type": "archive",
            "foundry_version": "1",
        }

    async def handle(request):
        state.calls.append(request)
        assert request.headers["Foundry-Features"] == "Skills=V1Preview"
        if state.outage:
            raise httpx.ConnectError("unreachable", request=request)
        if request.method == "GET":
            pins = [{"name": name, "version": "1"} for name in records]
            if state.defect == "floating":
                del pins[0]["version"]
            return httpx.Response(200, json={"skills": pins})
        body = json.loads(request.content)
        method = body["method"]
        if method == "notifications/initialized":
            return httpx.Response(204)
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25"}
        elif method == "resources/list":
            result = {
                "resources": [
                    {"uri": uri, "description": "Generic entry-document transport metadata"}
                    for uri in [
                        "skill://index.json",
                        *[f"skill://{name}/SKILL.md" for name in records],
                        *[f"skill://{name}.zip" for name in records],
                    ]
                ]
            }
        else:
            assert method == "resources/read"
            uri = body["params"]["uri"]
            if uri == "skill://index.json":
                text = json.dumps(
                    {
                        "skills": [
                            {**record, "url": f"skill://{name}.zip"}
                            for name, record in records.items()
                        ]
                    }
                )
                content = {"uri": uri, "text": text}
            else:
                name = uri.removeprefix("skill://").split("/")[0].removesuffix(".zip")
                files = snapshot.skill_files(name)
                if state.defect == "traversal":
                    files["../escape"] = b"bad"
                if uri.endswith(".zip"):
                    if state.defect == "archive-mismatch":
                        files["SKILL.md"] = b"changed instructions"
                    content = {
                        "uri": uri,
                        "blob": base64.b64encode(archive(files)).decode(),
                    }
                else:
                    text = files["SKILL.md"].decode()
                    if state.defect == "description":
                        text = text.replace("description: Use", "description: Different")
                    content = {"uri": uri, "text": text}
            if state.defect == "wrong-uri" and uri != "skill://index.json":
                content["uri"] = "skill://another/SKILL.md"
            result = {"contents": [content]}
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "test-session"},
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )

    state.factory = lambda value: ToolboxClient(
        value,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        credential=state.credential,
        attempts=1,
        cache_bytes=4096,
        cache_entries=2,
    )
    state.binding, state.records = binding, records
    state.source = {
        **binding,
        "kind": "foundry",
        "skill_mode": "provider",
        "toolbox_version": "1",
        "skill_versions": {name: "1" for name in records},
    }
    return state


def manifest_for(toolbox):
    return {
        "distribution": toolbox.source,
        "assignments": {"coder": ["dotnet-development", "test-strategy"]},
        "skills": {name: dict(record) for name, record in toolbox.records.items()},
    }


def resource_reads(toolbox):
    return [
        body["params"]["uri"]
        for request in toolbox.calls
        if request.method == "POST"
        and (body := json.loads(request.content))["method"] == "resources/read"
    ]


async def test_progressive_disclosure_materialization_cache_refresh_cleanup(toolbox):
    provider = ToolboxSkillProvider(manifest_for(toolbox), "coder", client_factory=toolbox.factory)
    root = provider.root
    try:
        descriptors = await provider.list_skills()
        assert {item.name for item in descriptors} == {"dotnet-development", "test-strategy"}
        assert resource_reads(toolbox) == ["skill://index.json"]
        assert all(Path(item.directory).is_absolute() for item in descriptors)
        assert list(root.iterdir()) == []
        text, repeated = await asyncio.gather(
            provider.read_skill("dotnet-development"), provider.read_skill("dotnet-development")
        )
        assert text == repeated
        assert (root / "dotnet-development/references/guide.md").read_text()
        assert not (root / "test-strategy").exists()
        assert not any("java-development" in uri for uri in resource_reads(toolbox))
        assert resource_reads(toolbox).count("skill://dotnet-development/SKILL.md") == 1
        assert provider.client.cache_size <= 4096 and len(provider.client.cache) <= 2
        assert toolbox.credential.calls == len(toolbox.calls)
        tokens = [request.headers["Authorization"] for request in toolbox.calls]
        assert len(set(tokens)) == len(tokens)
    finally:
        await provider.aclose()
    assert not root.exists() and toolbox.credential.closed


async def test_nonpermitted_read_never_fetches_and_is_fatal(toolbox):
    provider = ToolboxSkillProvider(manifest_for(toolbox), "coder", client_factory=toolbox.factory)
    try:
        await provider.list_skills()
        before = len(toolbox.calls)
        with pytest.raises(FactoryError, match="not permitted"):
            await provider.read_skill("java-development")
        assert len(toolbox.calls) == before
        original_failure = provider.failure
        with pytest.raises(FactoryError, match="not permitted"):
            await provider.read_skill("dotnet-development")
        assert provider.failure == original_failure
        with pytest.raises(FactoryError, match="refused"):
            provider.raise_if_failed()
    finally:
        await provider.aclose()


async def test_unlinked_scripts_are_materialized_without_executing_them(toolbox, snapshot):
    script = b"raise RuntimeError('must never execute')\n"
    snapshot.files["skills/dotnet-development/scripts/check.py"] = script
    provider = ToolboxSkillProvider(manifest_for(toolbox), "coder", client_factory=toolbox.factory)
    try:
        await provider.list_skills()
        await provider.read_skill("dotnet-development")
        assert (provider.root / "dotnet-development/scripts/check.py").read_bytes() == script
    finally:
        await provider.aclose()


@pytest.mark.parametrize("phase", ["startup", "mid-session"])
async def test_unreachable_toolbox_fails_without_fallback(toolbox, phase):
    provider = ToolboxSkillProvider(manifest_for(toolbox), "coder", client_factory=toolbox.factory)
    try:
        if phase == "mid-session":
            await provider.list_skills()
        toolbox.outage = True
        operation = (
            provider.list_skills() if phase == "startup" else provider.read_skill("test-strategy")
        )
        with pytest.raises(FactoryError, match="Toolbox transport unavailable.*no bridge fallback"):
            await operation
        assert not provider.loaded and list(provider.root.iterdir()) == []
    finally:
        await provider.aclose()


async def test_loaded_skill_survives_outage_but_next_activation_does_not(toolbox):
    provider = ToolboxSkillProvider(manifest_for(toolbox), "coder", client_factory=toolbox.factory)
    try:
        await provider.list_skills()
        original = await provider.read_skill("dotnet-development")
        toolbox.outage = True
        assert await provider.read_skill("dotnet-development") == original
        with pytest.raises(FactoryError, match="test-strategy"):
            await provider.read_skill("test-strategy")
    finally:
        await provider.aclose()


@pytest.mark.parametrize("defect", ["description", "archive-mismatch", "traversal", "wrong-uri"])
async def test_malformed_or_unsafe_activated_resource_fails_closed(toolbox, defect):
    provider = ToolboxSkillProvider(manifest_for(toolbox), "coder", client_factory=toolbox.factory)
    try:
        await provider.list_skills()
        toolbox.defect = defect
        with pytest.raises(FactoryError, match="refused"):
            await provider.read_skill("dotnet-development")
        assert not provider.loaded and list(provider.root.iterdir()) == []
    finally:
        await provider.aclose()


async def test_callback_deadline_cancels_network_and_releases_lock(toolbox):
    provider = ToolboxSkillProvider(
        manifest_for(toolbox), "coder", client_factory=toolbox.factory, deadline=0.01
    )
    cancelled = asyncio.Event()

    async def blocked():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    provider.client.catalog = blocked
    try:
        with pytest.raises(FactoryError, match="TimeoutError"):
            await provider.list_skills()
        assert cancelled.is_set() and not provider.lock.locked()
    finally:
        await provider.aclose()


async def test_cancelled_activation_does_not_finish_in_background(toolbox):
    provider = ToolboxSkillProvider(manifest_for(toolbox), "coder", client_factory=toolbox.factory)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(uri):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    try:
        await provider.list_skills()
        provider.client.read = blocked
        activation = asyncio.create_task(provider.read_skill("dotnet-development"))
        await entered.wait()
        activation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await activation
        assert cancelled.is_set() and not provider.loaded and not provider.lock.locked()
        assert list(provider.root.iterdir()) == []
        with pytest.raises(FactoryError, match="cancelled"):
            provider.raise_if_failed()
    finally:
        await provider.aclose()


def test_metadata_only_resolver_and_explicit_role_policy(toolbox, policy):
    resolver = ToolboxProviderResolver(toolbox.binding, client_factory=toolbox.factory)
    snapshot = resolver.resolve()
    assert isinstance(snapshot, ProviderSnapshot)
    assert resource_reads(toolbox) == ["skill://index.json"]
    with pytest.raises(FactoryError, match="explicit permitted_skills"):
        snapshot.assignments(policy, ["coder"])
    policy["permitted_skills"] = {"coder": ["dotnet-development"]}
    assert snapshot.assignments(policy, ["coder"]) == {"coder": ["dotnet-development"]}
    policy["permitted_skills"]["tester"] = ["test-strategy"]
    assert snapshot.assignments(policy, ["coder"]) == {"coder": ["dotnet-development"]}
    policy["permitted_skills"]["coder"] = ["missing"]
    with pytest.raises(FactoryError, match="unauthorized"):
        snapshot.assignments(policy, ["coder"])


def test_floating_toolbox_reference_stops_before_skill_reads(toolbox):
    toolbox.defect = "floating"
    with pytest.raises(FactoryError, match="floating"):
        ToolboxProviderResolver(toolbox.binding, client_factory=toolbox.factory).resolve()
    assert resource_reads(toolbox) == []


def test_invalid_role_cannot_be_used_as_a_directory(toolbox, policy):
    policy["permitted_skills"] = {"../escape": ["dotnet-development"]}
    with pytest.raises(FactoryError, match="Invalid provider roles"):
        ProviderSnapshot(toolbox.records, toolbox.source).assignments(policy, ["../escape"])


def test_role_resolver_pins_distinct_endpoints_and_preserves_role_allowlist(toolbox, policy):
    identity = {"tenant_id": "tenant", "client_id": "client", "principal_id": "principal"}
    bindings = {
        "roles": {
            role: {**toolbox.binding, "toolbox": f"{role}-skills", "identity": identity}
            for role in ("coder", "tester", "architect")
        }
    }
    resolved = ToolboxProviderResolver(
        bindings, roles=["coder", "tester"], client_factory=toolbox.factory
    ).resolve()
    assert set(resolved.source["role_bindings"]) == {"coder", "tester"}
    assert not any("architect-skills" in str(request.url) for request in toolbox.calls)
    policy["permitted_skills"] = {"coder": ["dotnet-development"], "tester": ["test-strategy"]}
    assert resolved.assignments(policy, ["coder", "tester"]) == policy["permitted_skills"]
    resolved.source["role_bindings"]["tester"]["skill_versions"] = {"test-strategy": "1"}
    policy["permitted_skills"]["tester"] = ["dotnet-development"]
    with pytest.raises(FactoryError, match="unauthorized"):
        resolved.assignments(policy, ["tester"])


async def test_role_provider_receives_only_its_toolbox_and_cannot_switch(toolbox):
    manifest = manifest_for(toolbox)
    manifest["distribution"] = {
        "role_bindings": {
            role: {**toolbox.source, "toolbox": f"{role}-skills"} for role in ("coder", "tester")
        }
    }
    provider = ToolboxSkillProvider(manifest, "coder", client_factory=toolbox.factory)
    try:
        await provider.list_skills()
        await provider.read_skill("dotnet-development")
        assert all("/toolboxes/coder-skills/" in str(request.url) for request in toolbox.calls)
    finally:
        await provider.aclose()
    manifest["distribution"]["role_bindings"]["coder"]["toolbox"] = "tester-skills"
    with pytest.raises(FactoryError, match="another role"):
        ToolboxSkillProvider(manifest, "coder", client_factory=toolbox.factory)


async def test_wrong_role_credential_is_rejected_before_http(toolbox):
    expected = {
        "tenant_id": "tenant",
        "client_id": "coder-client",
        "principal_id": "coder-principal",
    }
    claims = {
        "tid": "tenant",
        "appid": "tester-client",
        "oid": "tester-principal",
        "aud": "https://ai.azure.com",
    }

    class OtherRoleCredential(Credential):
        async def get_token(self, scope):
            payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            return SimpleNamespace(token=f"header.{payload}.signature")

    client = toolbox.factory({**toolbox.binding, "identity": expected})
    client.credential = OtherRoleCredential()
    try:
        with pytest.raises(FactoryError, match="pinned role identity"):
            await client.request("GET", client.base)
        assert toolbox.calls == []
        claims.update(appid="coder-client", oid="coder-principal")
        assert (await client.request("GET", client.base)).status_code == 200
        assert client.calls[-1]["identity"]["principal_id"] == "coder-principal"
    finally:
        await client.aclose()


async def test_retry_backoff_is_bounded_and_authorization_is_not_retried(toolbox, monkeypatch):
    sleeps = []

    async def sleep(value):
        sleeps.append(value)

    monkeypatch.setattr("ai_factory.provider.asyncio.sleep", sleep)
    statuses = [503, 429, 200, 403]
    client = ToolboxClient(
        toolbox.binding,
        credential=toolbox.credential,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(statuses.pop(0)))
        ),
    )
    try:
        assert (await client.request("GET", client.base)).status_code == 200
        assert sleeps == [0.25, 0.5]
        with pytest.raises(FactoryError, match="403"):
            await client.request("GET", client.base)
        assert len(client.calls) == 4
    finally:
        await client.aclose()


async def test_provider_runtime_keeps_policy_records_native_activation_and_cleans_up(
    toolbox, target, request_data, policy, tmp_path
):
    policy["permitted_skills"] = {"coder": ["dotnet-development"]}
    snapshot = ProviderSnapshot(toolbox.records, toolbox.source)
    directory = prepare(
        target, snapshot, tmp_path / "runs", request_data, policy, ["coder"], ["implementation"]
    )
    assert not (directory / "skills").exists()
    original = (target / "OrderService.cs").read_bytes()

    class ProviderClient(FakeClient):
        async def create_session(self, **config):
            provider = config["skill_provider"]
            assert config["skill_directories"] == []
            await provider.list_skills()
            await provider.read_skill("dotnet-development")
            return await super().create_session(**config)

    await run(
        directory,
        "test-model",
        client_factory=ProviderClient,
        provider_factory=partial(ToolboxSkillProvider, client_factory=toolbox.factory),
    )
    manifest = verify(directory)
    assert manifest["activations"] == [
        {"role": "coder", "skill": "dotnet-development", "native_event_type": "skill.invoked"}
    ]
    assert manifest["skills"]["dotnet-development"]["files"]["references/guide.md"]
    metrics = json.loads((directory / "coder.provider-metrics.json").read_text())
    assert metrics["temporary_directory_removed"]
    assert not Path(metrics["temporary_directory"]).exists()
    assert (target / "OrderService.cs").read_bytes() == original
    assert not (directory / "skills").exists()
    assert manifest["skills"]["dotnet-development"]["sha256"] == digest(
        canonical(manifest["skills"]["dotnet-development"]["files"])
    )
    tools = WorkerTools(directory, manifest, "coder")
    assert (
        tools.permission(SimpleNamespace(kind="read", tool_name="view"), {}).kind
        == "denied-by-rules"
    )


@pytest.mark.parametrize("phase", ["startup", "mid-session"])
async def test_runtime_refuses_success_after_provider_failure(
    toolbox, target, request_data, policy, tmp_path, phase
):
    policy["permitted_skills"] = {"coder": ["dotnet-development"]}
    directory = prepare(
        target,
        ProviderSnapshot(toolbox.records, toolbox.source),
        tmp_path / "runs",
        request_data,
        policy,
        ["coder"],
        ["implementation"],
    )

    class FailureClient(FakeClient):
        async def create_session(self, **config):
            provider = config["skill_provider"]
            if phase == "startup":
                toolbox.outage = True
                try:
                    await provider.list_skills()
                except FactoryError:
                    raise RuntimeError("SDK sanitized provider exception") from None
            await provider.list_skills()

            class Session:
                async def send_and_wait(self, prompt, timeout):
                    toolbox.outage = True
                    try:
                        await provider.read_skill("dotnet-development")
                    except FactoryError:
                        pass
                    tool = next(
                        item for item in config["tools"] if item.name == "read_project_file"
                    )
                    result = tool.handler(SimpleNamespace(arguments={"path": "OrderService.cs"}))
                    assert result.result_type == "failure"
                    assert "Toolbox transport unavailable" in result.text_result_for_llm
                    return SimpleNamespace(data=SimpleNamespace(content="Continue without skill"))

                async def disconnect(self):
                    pass

            return Session()

    with pytest.raises(FactoryError, match="Toolbox transport unavailable"):
        await run(
            directory,
            "test-model",
            client_factory=FailureClient,
            provider_factory=partial(ToolboxSkillProvider, client_factory=toolbox.factory),
        )
    assert not (directory / "coder.result.json").exists()
    assert not (directory / "proposals").exists()
    assert not (directory / ".worker.lock").exists()
    metrics = json.loads((directory / "coder.provider-metrics.json").read_text())
    assert metrics["temporary_directory_removed"] and metrics["failure"]
