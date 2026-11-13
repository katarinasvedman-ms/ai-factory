from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote, urlsplit

import httpx

from .contracts import MAX_BYTES, FactoryError, Snapshot, canonical, digest, local_files, unpack_zip

if TYPE_CHECKING:
    from .provider import ProviderSnapshot


class SkillResolver(Protocol):
    def resolve(self) -> Snapshot | ProviderSnapshot: ...


class LocalResolver:
    def __init__(self, root: Path):
        self.root = root

    def resolve(self) -> Snapshot:
        files = local_files(self.root)
        reference = digest(canonical({k: digest(v) for k, v in sorted(files.items())}))
        return Snapshot(files, {"kind": "local", "reference": reference})


class Transport:
    def __init__(self, client: httpx.Client | None = None, attempts: int = 3):
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.attempts = attempts

    def request(self, method: str, url: str, *, allowed_statuses=(), **kwargs) -> httpx.Response:
        for attempt in range(self.attempts):
            try:
                # Bound response bodies as well as extracted archives.
                with self.client.stream(method, url, **kwargs) as response:
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise FactoryError("Response exceeds size limit")
                    result = httpx.Response(
                        response.status_code,
                        headers={
                            key: value
                            for key, value in response.headers.items()
                            if key not in {"content-encoding", "content-length"}
                        },
                        content=bytes(data),
                        request=response.request,
                    )
                if result.status_code in {429, 500, 502, 503, 504} and attempt + 1 < self.attempts:
                    time.sleep(min(2**attempt, 4))
                    continue
                if result.status_code not in allowed_statuses:
                    result.raise_for_status()
                return result
            except httpx.TransportError:
                if attempt + 1 == self.attempts:
                    raise FactoryError("Distribution transport unavailable") from None
                time.sleep(min(2**attempt, 4))
            except httpx.HTTPStatusError as exc:
                # Do not expose headers, tokens, signed URLs, or response bodies.
                raise FactoryError(f"Distribution HTTP {exc.response.status_code}") from None
        raise FactoryError("Distribution retries exhausted")


class GitHubResolver:
    def __init__(
        self,
        repository: str,
        commit: str,
        transport: Transport | None = None,
        token: str | None = None,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise FactoryError("Invalid GitHub repository")
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise FactoryError("GitHub source must be an approved full commit SHA")
        self.repository, self.commit = repository, commit
        self.transport, self.token = transport or Transport(), token

    def resolve(self) -> Snapshot:
        token = self.token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=15, check=False
            )
            if result.returncode:
                raise FactoryError("GitHub authentication unavailable")
            token = result.stdout.strip()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        response = self.transport.request(
            "GET",
            f"https://api.github.com/repos/{self.repository}/zipball/{self.commit}",
            headers=headers,
            allowed_statuses={301, 302, 307},
        )
        if response.is_redirect:
            location = response.headers.get("location", "")
            parsed = urlsplit(location)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "codeload.github.com"
                or self.commit not in parsed.path.split("/")
            ):
                raise FactoryError("Untrusted GitHub archive redirect")
            response = self.transport.request("GET", location)
        return self._snapshot(response.content)

    def _snapshot(self, archive: bytes) -> Snapshot:
        return Snapshot(
            unpack_zip(archive, strip_root=True),
            {
                "kind": "github",
                "repository": self.repository,
                "commit": self.commit,
            },
        )


class FoundryResolver:
    """Toolbox discovery plus pinned full-package download into native skill roots."""

    def __init__(
        self, approved: Snapshot, binding: dict, transport: Transport | None = None, credential=None
    ):
        endpoint = binding["project_endpoint"].rstrip("/")
        if not re.fullmatch(
            r"https://[a-zA-Z0-9-]+\.services\.ai\.azure\.com/api/projects/[a-zA-Z0-9_-]+",
            endpoint,
        ):
            raise FactoryError("Expected an Azure Foundry public-cloud project endpoint")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", binding["toolbox"]):
            raise FactoryError("Invalid toolbox name")
        versions = [binding["version"], *binding["skills"].values()]
        if any(not isinstance(v, str) or not re.fullmatch(r"[0-9]+", v) for v in versions):
            raise FactoryError(
                "Foundry toolbox and skill versions must be explicit numeric strings"
            )
        if set(binding["skills"]) != set(approved.entries):
            raise FactoryError("Foundry binding must map every approved catalog skill")
        self.approved, self.binding, self.endpoint = approved, binding, endpoint
        self.transport, self.credential = transport or Transport(), credential
        self.mcp_headers: dict[str, str] = {}
        self.request_id = 0

    def headers(self) -> dict:
        if self.credential is None:
            from azure.identity import DefaultAzureCredential

            self.credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        token = self.credential.get_token("https://ai.azure.com/.default").token
        return {
            "Authorization": f"Bearer {token}",
            "Foundry-Features": "Skills=V1Preview",
            "Accept": "application/json, text/event-stream",
            **self.mcp_headers,
        }

    def rpc(self, method: str, params: dict | None = None, *, notification=False) -> dict:
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            self.request_id += 1
            body["id"] = self.request_id
        url = (
            f"{self.endpoint}/toolboxes/{self.binding['toolbox']}/versions/"
            f"{self.binding['version']}/mcp?api-version=v1"
        )
        response = self.transport.request("POST", url, headers=self.headers(), json=body)
        if response.headers.get("mcp-session-id"):
            self.mcp_headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        if notification:
            return {}
        if "text/event-stream" in response.headers.get("content-type", ""):
            messages = [
                json.loads(block) for block in re.findall(r"(?m)^data: (.+)$", response.text)
            ]
            message = next((m for m in messages if m.get("id") == body["id"]), {})
        else:
            message = response.json()
        if message.get("id") != body["id"] or "error" in message or "result" not in message:
            raise FactoryError(f"Foundry MCP {method} failed")
        return message["result"]

    def resolve(self) -> Snapshot:
        base = f"{self.endpoint}/toolboxes/{self.binding['toolbox']}/versions"
        version = self.transport.request(
            "GET", f"{base}/{self.binding['version']}?api-version=v1", headers=self.headers()
        ).json()
        references = {s["name"]: s.get("version") for s in version.get("skills", [])}
        if any(references.get(name) != number for name, number in self.binding["skills"].items()):
            raise FactoryError("Toolbox contains floating, missing, or unexpected skill versions")
        initialized = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ai-factory", "version": "0.1.0"},
            },
        )
        self.mcp_headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        self.rpc("notifications/initialized", notification=True)
        resources, cursors, cursor = {}, set(), None
        for _ in range(100):
            page = self.rpc("resources/list", {"cursor": cursor} if cursor else {})
            for resource in page.get("resources", []):
                resources[resource["uri"]] = resource
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if cursor in cursors:
                raise FactoryError("Repeated Foundry resource cursor")
            cursors.add(cursor)
        else:
            raise FactoryError("Foundry resource pagination exceeded limit")
        files = {"catalog.json": self.approved.files["catalog.json"]}
        for name, number in sorted(self.binding["skills"].items()):
            uri = f"skill://{name}/SKILL.md"
            if uri not in resources:
                raise FactoryError(f"Toolbox did not advertise required skill: {name}")
            resource = self.rpc("resources/read", {"uri": uri})
            texts = [c["text"] for c in resource.get("contents", []) if "text" in c]
            expected = self.approved.skill_files(name)
            if texts != [expected["SKILL.md"].decode("utf-8")]:
                raise FactoryError(f"Toolbox resource differs from approved instructions: {name}")
            package = self.transport.request(
                "GET",
                f"{self.endpoint}/skills/{quote(name)}/versions/{number}/content?api-version=v1",
                headers={**self.headers(), "Accept": "application/zip"},
            )
            downloaded = unpack_zip(package.content)
            if "SKILL.md" not in downloaded:
                prefix = name + "/"
                if not all(path.startswith(prefix) for path in downloaded):
                    raise FactoryError(f"Unsupported Foundry package layout: {name}")
                downloaded = {path[len(prefix) :]: data for path, data in downloaded.items()}
            if downloaded != expected:
                raise FactoryError(f"Foundry package integrity mismatch: {name}")
            files.update({f"skills/{name}/{path}": data for path, data in downloaded.items()})
        return Snapshot(
            files,
            {
                "kind": "foundry",
                "project_endpoint": self.endpoint,
                "toolbox": self.binding["toolbox"],
                "toolbox_version": self.binding["version"],
                "skill_versions": self.binding["skills"],
                "approved_catalog_sha256": self.approved.catalog_sha256,
            },
        )
