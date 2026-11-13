import io
import json
import zipfile
from types import SimpleNamespace

import httpx
import pytest

from ai_factory.contracts import FactoryError, relative_path, unpack_zip
from ai_factory.resolvers import FoundryResolver, GitHubResolver, Transport


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.filename = name
            bundle.writestr(info, data)
    return buffer.getvalue()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a\\b", "C:/file", "CON", "a/../b"])
def test_paths_cannot_escape_or_alias(path):
    with pytest.raises(FactoryError):
        relative_path(path)
    with pytest.raises(FactoryError):
        unpack_zip(archive({path: b"bad"}))


def test_archive_rejects_case_collisions_and_links():
    with pytest.raises(FactoryError, match="colliding"):
        unpack_zip(archive({"a": b"a", "A": b"b"}))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        info = zipfile.ZipInfo("link")
        info.external_attr = 0o120777 << 16
        bundle.writestr(info, "../../escape")
    with pytest.raises(FactoryError, match="link"):
        unpack_zip(buffer.getvalue())


def test_github_pins_commit_and_does_not_forward_token(snapshot):
    commit = "a" * 40
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(
                302,
                headers={
                    "Location": f"https://codeload.github.com/owner/skills/legacy.zip/{commit}"
                },
            )
        return httpx.Response(
            200, content=archive({"root/" + name: data for name, data in snapshot.files.items()})
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    resolved = GitHubResolver(
        "owner/skills", commit, Transport(client), "secret-test-token"
    ).resolve()
    assert resolved.records == snapshot.records
    assert resolved.source["commit"] == commit
    assert "authorization" in seen[0].headers
    assert "authorization" not in seen[1].headers
    with pytest.raises(FactoryError, match="commit"):
        GitHubResolver("owner/skills", "main")


def test_distribution_retries_only_transient_errors(monkeypatch):
    monkeypatch.setattr("ai_factory.resolvers.time.sleep", lambda _: None)
    attempts = []

    def handle(request):
        attempts.append(request)
        return httpx.Response(503)

    transport = Transport(httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(FactoryError, match="503"):
        transport.request("GET", "https://example.com/skills")
    assert len(attempts) == 3
    attempts.clear()
    transport = Transport(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: attempts.append(request) or httpx.Response(403)
            )
        )
    )
    with pytest.raises(FactoryError, match="403"):
        transport.request("GET", "https://example.com/skills")
    assert len(attempts) == 1


def test_bridge_unreachable_at_startup_fails_before_sdk(snapshot):
    def unavailable(request):
        raise httpx.ConnectError("controlled outage", request=request)

    resolver = FoundryResolver(
        snapshot,
        {
            "project_endpoint": "https://test.services.ai.azure.com/api/projects/test",
            "toolbox": "factory",
            "version": "1",
            "skills": {name: "1" for name in snapshot.entries},
        },
        Transport(httpx.Client(transport=httpx.MockTransport(unavailable)), attempts=1),
        SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="test")),
    )
    with pytest.raises(FactoryError, match="Distribution transport unavailable"):
        resolver.resolve()


@pytest.mark.parametrize("defect", [None, "floating", "tampered", "missing", "resource"])
def test_foundry_uses_mcp_resources_and_pinned_packages(snapshot, defect):
    calls = []
    binding = {
        "project_endpoint": "https://test.services.ai.azure.com/api/projects/test",
        "toolbox": "factory",
        "version": "3",
        "skills": {name: "2" for name in snapshot.entries},
    }

    def handle(request):
        calls.append(request)
        path = request.url.path
        if request.method == "GET" and "/toolboxes/" in path:
            references = [
                {"name": name, "version": number} for name, number in binding["skills"].items()
            ]
            if defect == "floating":
                del references[0]["version"]
            return httpx.Response(200, json={"skills": references})
        if request.method == "GET":
            assert "/versions/2/content" in path
            name = path.split("/skills/")[1].split("/")[0]
            files = snapshot.skill_files(name)
            if defect == "tampered":
                files["references/guide.md"] = b"Tampered"
            return httpx.Response(200, content=archive(files))
        message = json.loads(request.content)
        method = message["method"]
        assert "/versions/3/mcp" in path
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26"}
        elif method == "resources/list":
            result = {
                "resources": [
                    {"uri": f"skill://{name}/SKILL.md"}
                    for name in snapshot.entries
                    if defect != "missing"
                ]
            }
        else:
            assert method == "resources/read"
            name = message["params"]["uri"].split("//")[1].split("/")[0]
            text = snapshot.skill_files(name)["SKILL.md"].decode()
            result = {"contents": [{"text": text if defect != "resource" else "Tampered"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="not-a-secret"))
    transport = Transport(httpx.Client(transport=httpx.MockTransport(handle)))
    resolver = FoundryResolver(snapshot, binding, transport, credential)
    if defect:
        with pytest.raises(FactoryError):
            resolver.resolve()
    else:
        resolved = resolver.resolve()
        assert resolved.records == snapshot.records
        assert resolved.source["toolbox_version"] == "3"
        methods = [json.loads(c.content)["method"] for c in calls if c.method == "POST"]
        assert "resources/list" in methods and "resources/read" in methods
        assert "tools/list" not in methods
