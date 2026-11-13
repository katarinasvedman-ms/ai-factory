from __future__ import annotations

import asyncio
import base64
import json
import re
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from copilot import SkillProviderDescriptor

from .contracts import (
    MAX_BYTES,
    MAX_FILES,
    NAME,
    ROLES,
    FactoryError,
    canonical,
    digest,
    unpack_zip,
)


class ToolboxClient:
    """A session-scoped, bounded MCP resource client; no local approval tree."""

    def __init__(
        self,
        binding: dict,
        *,
        client=None,
        credential=None,
        attempts=3,
        cache_entries=32,
        cache_bytes=4 * 1024 * 1024,
    ):
        self.endpoint = binding["project_endpoint"].rstrip("/")
        self.toolbox = binding["toolbox"]
        self.version = binding.get("toolbox_version", binding.get("version"))
        self.identity = binding.get("identity")
        if not re.fullmatch(
            r"https://[a-zA-Z0-9-]+\.services\.ai\.azure\.com/api/projects/[a-zA-Z0-9_-]+",
            self.endpoint,
        ):
            raise FactoryError("Expected an Azure Foundry public-cloud project endpoint")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", self.toolbox):
            raise FactoryError("Invalid toolbox name")
        if not isinstance(self.version, str) or not re.fullmatch(r"[0-9]+", self.version):
            raise FactoryError("Toolbox version must be an explicit numeric string")
        if not 1 <= attempts <= 3 or cache_entries < 0 or cache_bytes < 0:
            raise FactoryError("Invalid Toolbox retry/cache limits")
        self.client = client or httpx.AsyncClient(timeout=6, follow_redirects=False)
        if credential is None:
            from azure.identity.aio import DefaultAzureCredential

            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        self.credential = credential
        self.attempts = attempts
        self.cache_entries, self.cache_limit = cache_entries, cache_bytes
        self.cache: OrderedDict[str, bytes] = OrderedDict()
        self.cache_size = 0
        self.cache_hits = 0
        self.calls: list[dict] = []
        self.headers: dict[str, str] = {}
        self.request_id = 0
        self.versions: dict[str, str] | None = None

    @property
    def base(self):
        return f"{self.endpoint}/toolboxes/{self.toolbox}/versions/{self.version}"

    async def request(self, method, url, *, body=None):
        operation = body.get("method") if body else "toolbox.version"
        uri = body.get("params", {}).get("uri") if body else None
        for attempt in range(self.attempts):
            row = {
                "operation": operation,
                "uri": uri,
                "attempt": attempt + 1,
                "bytes": 0,
                "toolbox": self.toolbox,
                "toolbox_version": self.version,
            }
            self.calls.append(row)
            started = time.perf_counter()
            try:
                try:
                    token = await self.credential.get_token("https://ai.azure.com/.default")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise FactoryError("Toolbox authentication unavailable") from None
                if self.identity:
                    try:
                        payload = token.token.split(".")[1]
                        claims = json.loads(
                            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
                        )
                    except (ValueError, IndexError):
                        raise FactoryError("Cannot identify the role's Entra credential") from None
                    if (
                        claims.get("tid") != self.identity["tenant_id"]
                        or claims.get("oid") != self.identity["principal_id"]
                        or (claims.get("appid") or claims.get("azp")) != self.identity["client_id"]
                        or claims.get("aud") != "https://ai.azure.com"
                    ):
                        raise FactoryError(
                            "Toolbox credential does not match the pinned role identity"
                        )
                    # Routing check only; the Azure endpoint validates the token's signature.
                    row["identity"] = {
                        "tenant_id": claims["tid"],
                        "principal_id": claims["oid"],
                        "client_id": claims.get("appid") or claims.get("azp"),
                        "audience": claims["aud"],
                    }
                headers = {
                    **self.headers,
                    "Authorization": f"Bearer {token.token}",
                    "Foundry-Features": "Skills=V1Preview",
                    "Accept": "application/json, text/event-stream",
                }
                async with self.client.stream(method, url, headers=headers, json=body) as response:
                    row["status"] = response.status_code
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        row["bytes"] += len(chunk)
                        if len(data) + len(chunk) > MAX_BYTES:
                            raise FactoryError("Toolbox response exceeds size limit")
                        data.extend(chunk)
                    result = httpx.Response(
                        response.status_code,
                        content=bytes(data),
                        headers={
                            k: v
                            for k, v in response.headers.items()
                            if k not in {"content-encoding", "content-length"}
                        },
                    )
                if result.status_code in {429, 500, 502, 503, 504} and attempt + 1 < self.attempts:
                    await asyncio.sleep(0.25 * 2**attempt)
                    continue
                if not 200 <= result.status_code < 300:
                    raise FactoryError(f"Toolbox HTTP {result.status_code}")
                return result
            except httpx.TransportError:
                row["error"] = "transport-unavailable"
                if attempt + 1 == self.attempts:
                    raise FactoryError("Toolbox transport unavailable") from None
                await asyncio.sleep(0.25 * 2**attempt)
            finally:
                row["seconds"] = time.perf_counter() - started
        raise FactoryError("Toolbox retries exhausted")

    async def rpc(self, method, params=None, *, notification=False):
        self.request_id += 1
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            body["id"] = self.request_id
        response = await self.request("POST", f"{self.base}/mcp?api-version=v1", body=body)
        if response.headers.get("mcp-session-id"):
            self.headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        if notification:
            return {}
        if "text/event-stream" in response.headers.get("content-type", ""):
            messages = [json.loads(data) for data in re.findall(r"(?m)^data: (.+)$", response.text)]
            message = next((item for item in messages if item.get("id") == body["id"]), {})
        else:
            message = response.json()
        if message.get("id") != body["id"] or "error" in message or "result" not in message:
            raise FactoryError(f"Toolbox MCP {method} failed")
        return message["result"]

    async def open(self):
        if self.versions is not None:
            return
        version = (await self.request("GET", f"{self.base}?api-version=v1")).json()
        versions = {}
        for reference in version.get("skills", []):
            name, number = reference.get("name", ""), reference.get("version")
            if (
                not NAME.fullmatch(name)
                or len(name) > 64
                or name in versions
                or not isinstance(number, str)
                or not re.fullmatch(r"[0-9]+", number)
            ):
                raise FactoryError("Toolbox contains invalid, duplicate, or floating skill pins")
            versions[name] = number
        if not versions:
            raise FactoryError("Toolbox has no pinned skills")
        initialized = await self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ai-factory-provider", "version": "0.1.0"},
            },
        )
        self.headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        await self.rpc("notifications/initialized", notification=True)
        self.versions = versions

    async def read(self, uri):
        if uri in self.cache:
            self.cache.move_to_end(uri)
            self.cache_hits += 1
            return self.cache[uri]
        result = await self.rpc("resources/read", {"uri": uri})
        contents = result.get("contents", [])
        if len(contents) != 1 or contents[0].get("uri") != uri:
            raise FactoryError("Toolbox returned an ambiguous resource")
        item = contents[0]
        if isinstance(item.get("text"), str) and "blob" not in item:
            content = item["text"].encode("utf-8")
        elif isinstance(item.get("blob"), str) and "text" not in item:
            content = base64.b64decode(item["blob"], validate=True)
        else:
            raise FactoryError("Toolbox returned an unsupported resource encoding")
        if self.cache_entries and len(content) <= self.cache_limit:
            while self.cache and (
                len(self.cache) >= self.cache_entries
                or self.cache_size + len(content) > self.cache_limit
            ):
                _, removed = self.cache.popitem(last=False)
                self.cache_size -= len(removed)
            self.cache[uri] = content
            self.cache_size += len(content)
        return content

    async def catalog(self):
        await self.open()
        resources, cursors, cursor = {}, set(), None
        for _ in range(100):
            page = await self.rpc("resources/list", {"cursor": cursor} if cursor else {})
            for item in page.get("resources", []):
                if item["uri"] in resources:
                    raise FactoryError("Duplicate Toolbox resource")
                resources[item["uri"]] = item
                if len(resources) > MAX_FILES:
                    raise FactoryError("Toolbox resource count exceeds limit")
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if cursor in cursors:
                raise FactoryError("Repeated Toolbox resource cursor")
            cursors.add(cursor)
        else:
            raise FactoryError("Toolbox pagination exceeded limit")
        if "skill://index.json" not in resources:
            raise FactoryError("Toolbox discovery index is required")
        index = json.loads(await self.read("skill://index.json"))
        records = {}
        for item in index.get("skills", []):
            name, description = item.get("name", ""), item.get("description")
            kind = item.get("type")
            uri = f"skill://{name}.zip" if kind == "archive" else f"skill://{name}/SKILL.md"
            if (
                not NAME.fullmatch(name)
                or len(name) > 64
                or name in records
                or name not in self.versions
                or not isinstance(description, str)
                or not 1 <= len(description) <= 1024
                or kind not in {"skill-md", "archive"}
                or item.get("url") != uri
                or uri not in resources
                or f"skill://{name}/SKILL.md" not in resources
            ):
                raise FactoryError("Invalid Toolbox discovery metadata")
            records[name] = {
                "name": name,
                "description": description,
                "type": kind,
                "foundry_version": self.versions[name],
            }
        if set(records) != set(self.versions) or len(records) > 1024:
            raise FactoryError("Toolbox index and pinned references disagree")
        return records

    def metrics(self):
        return {
            "calls": self.calls,
            "bytes": sum(row["bytes"] for row in self.calls),
            "cache_hits": self.cache_hits,
            "cache_bytes": self.cache_size,
        }

    async def aclose(self):
        try:
            await self.client.aclose()
        finally:
            await self.credential.close()
            self.cache.clear()
            self.cache_size = 0


@dataclass
class ProviderSnapshot:
    records: dict
    source: dict

    @property
    def catalog_sha256(self):
        return digest(canonical(self.records))

    def assignments(self, policy, roles):
        if not roles or len(roles) != len(set(roles)) or not set(roles) <= ROLES:
            raise FactoryError("Invalid provider roles")
        permitted = policy.get("permitted_skills")
        if not isinstance(permitted, dict) or not set(roles) <= set(permitted) <= ROLES:
            raise FactoryError("Provider mode needs explicit permitted_skills for every role")
        result = {}
        for role in roles:
            names = permitted[role]
            role_bindings = self.source.get("role_bindings")
            available = (
                role_bindings.get(role, {}).get("skill_versions", {})
                if role_bindings is not None
                else self.records
            )
            if (
                not isinstance(names, list)
                or not names
                or any(not isinstance(name, str) for name in names)
                or len(names) != len(set(names))
                or not set(names) <= set(policy["allowed_skills"]) & set(available)
                or not set(policy.get("required_skills", {}).get(role, [])) <= set(names)
            ):
                raise FactoryError(f"Invalid or unauthorized provider skills for role: {role}")
            result[role] = sorted(names)
        return result


class ToolboxProviderResolver:
    def __init__(self, binding, *, client_factory=ToolboxClient, roles=None):
        self.binding, self.client_factory = binding, client_factory
        self.roles = roles
        self.metrics = {}

    def resolve(self) -> ProviderSnapshot:
        if "roles" in self.binding:
            return self._resolve_roles()
        return asyncio.run(self._resolve())

    def _resolve_roles(self):
        configured = self.binding["roles"]
        roles = self.roles or sorted(configured)
        if not roles or not set(roles) <= set(configured) <= ROLES:
            raise FactoryError("Missing or invalid role Toolbox bindings")
        projects = {configured[role]["project_endpoint"].rstrip("/") for role in roles}
        if len(projects) != 1:
            raise FactoryError("Role toolboxes must use the same Foundry project")
        records, sources, calls = {}, {}, []
        for role in roles:
            binding = configured[role]
            if binding["toolbox"] != f"{role}-skills" or "identity" not in binding:
                raise FactoryError("Role toolbox and explicit identity are required")
            # Operator discovery is separate from the pinned worker identity.
            resolver = ToolboxProviderResolver(
                {key: value for key, value in binding.items() if key != "identity"},
                client_factory=self.client_factory,
            )
            snapshot = resolver.resolve()
            for name, record in snapshot.records.items():
                if name in records and records[name] != record:
                    raise FactoryError("Shared role skill pins or descriptors disagree")
                records[name] = record
            sources[role] = {**snapshot.source, "identity": binding["identity"]}
            calls.extend(resolver.metrics["calls"])
        self.metrics = {"calls": calls, "bytes": sum(row["bytes"] for row in calls)}
        return ProviderSnapshot(
            records,
            {
                "kind": "foundry",
                "skill_mode": "provider",
                "project_endpoint": next(iter(projects)),
                "role_bindings": sources,
                "skill_versions": {
                    name: record["foundry_version"] for name, record in records.items()
                },
            },
        )

    async def _resolve(self):
        client = self.client_factory(self.binding)
        try:
            async with asyncio.timeout(24):
                records = await client.catalog()
            return ProviderSnapshot(
                records,
                {
                    "kind": "foundry",
                    "skill_mode": "provider",
                    "project_endpoint": client.endpoint,
                    "toolbox": client.toolbox,
                    "toolbox_version": client.version,
                    "skill_versions": client.versions,
                },
            )
        except TimeoutError:
            raise FactoryError("Toolbox discovery exceeded its 24-second deadline") from None
        finally:
            self.metrics = client.metrics()
            await client.aclose()


class ToolboxSkillProvider:
    def __init__(
        self,
        manifest,
        role,
        *,
        client_factory=ToolboxClient,
        on_materialized=lambda name, hashes: None,
        on_failure=lambda message: None,
        deadline=24,
    ):
        self.manifest, self.role = manifest, role
        self.names = frozenset(manifest["assignments"][role])
        source = manifest["distribution"]
        if "role_bindings" in source:
            if role not in source["role_bindings"]:
                raise FactoryError("No Toolbox endpoint configured for this role")
            self.distribution = source["role_bindings"][role]
            if self.distribution["toolbox"] != f"{role}-skills":
                raise FactoryError("Role provider cannot use another role's Toolbox endpoint")
        else:
            self.distribution = source
        self.client = client_factory(self.distribution)
        self.temporary = tempfile.TemporaryDirectory(prefix="factory-skills-")
        self.root = Path(self.temporary.name).resolve()
        self.on_materialized, self.on_failure = on_materialized, on_failure
        self.deadline = deadline
        self.lock = asyncio.Lock()
        self.loaded: set[str] = set()
        self.instruction_hashes: dict[str, str] = {}
        self.materialized_bytes = self.materialized_files = 0
        self.failure: str | None = None

    def raise_if_failed(self):
        if self.failure:
            raise FactoryError(self.failure)

    def fail(self, operation, name=None, reason="unavailable"):
        if self.failure:
            return
        self.failure = (
            f"Toolbox skill provider {operation} failed for role {self.role}"
            + (f", skill {name}" if name in self.names else "")
            + f": {reason}; activation/run refused, no bridge fallback"
        )
        self.on_failure(self.failure)

    async def list_skills(self):
        try:
            async with asyncio.timeout(self.deadline), self.lock:
                self.raise_if_failed()
                records = await self.client.catalog()
                if self.client.versions != self.distribution["skill_versions"]:
                    raise FactoryError("Pinned Toolbox references changed")
                for name in self.names:
                    expected = self.manifest["skills"][name]
                    if records.get(name) != {key: expected[key] for key in records[name]}:
                        raise FactoryError("Pinned discovery metadata changed")
                return [
                    SkillProviderDescriptor(
                        name=name,
                        description=records[name]["description"],
                        directory=str(self.root / name),
                    )
                    for name in sorted(self.names)
                ]
        except asyncio.CancelledError:
            self.fail("list", reason="cancelled")
            raise
        except Exception as exc:
            self.fail(
                "list", reason=str(exc) if isinstance(exc, FactoryError) else type(exc).__name__
            )
            raise FactoryError(self.failure) from exc

    async def read_skill(self, name):
        try:
            async with asyncio.timeout(self.deadline), self.lock:
                self.raise_if_failed()
                if name not in self.names:
                    raise FactoryError("Skill is not permitted for this role")
                if name in self.loaded:
                    content = (self.root / name / "SKILL.md").read_bytes()
                    if digest(content) != self.instruction_hashes[name]:
                        raise FactoryError("Materialized skill instructions changed")
                    return content.decode("utf-8")
                if self.client.versions is None:
                    raise FactoryError("Skill catalog must be listed before activation")
                instructions = await self.client.read(f"skill://{name}/SKILL.md")
                if len(instructions) > 1024 * 1024:
                    raise FactoryError("Skill instructions exceed the SDK limit")
                text = instructions.decode("utf-8")
                sections = text.split("---", 2)
                front = yaml.safe_load(sections[1]) if len(sections) == 3 else None
                expected = self.manifest["skills"][name]
                if (
                    not isinstance(front, dict)
                    or sections[0].strip()
                    or not sections[2].strip()
                    or front.get("name") != name
                    or front.get("description") != expected["description"]
                    or any(
                        key in front
                        for key in ("argument-hint", "user-invocable", "disable-model-invocation")
                    )
                ):
                    raise FactoryError("Skill frontmatter disagrees with discovery metadata")
                files = {"SKILL.md": instructions}
                if expected["type"] == "archive":
                    files = unpack_zip(await self.client.read(f"skill://{name}.zip"))
                    if "SKILL.md" not in files:
                        prefix = name + "/"
                        if not files or not all(path.startswith(prefix) for path in files):
                            raise FactoryError("Unsupported Toolbox package layout")
                        files = {path[len(prefix) :]: data for path, data in files.items()}
                    if files.get("SKILL.md") != instructions:
                        raise FactoryError("Toolbox archive and instructions disagree")
                hashes = {path: digest(data) for path, data in sorted(files.items())}
                if "files" in expected and hashes != expected["files"]:
                    raise FactoryError("Previously activated pinned skill bytes changed")
                size = sum(map(len, files.values()))
                if (
                    self.materialized_bytes + size > MAX_BYTES
                    or self.materialized_files + len(files) > MAX_FILES
                ):
                    raise FactoryError("Session supporting resources exceed limits")
                root = self.root / name
                for path, data in files.items():
                    destination = root / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                self.on_materialized(name, hashes)
                self.materialized_bytes += size
                self.materialized_files += len(files)
                self.loaded.add(name)
                self.instruction_hashes[name] = digest(instructions)
                return text
        except asyncio.CancelledError:
            self.fail("read", name, "cancelled")
            raise
        except Exception as exc:
            self.fail(
                "read", name, str(exc) if isinstance(exc, FactoryError) else type(exc).__name__
            )
            raise FactoryError(self.failure) from exc

    async def aclose(self):
        try:
            await self.client.aclose()
        finally:
            self.temporary.cleanup()
            self.loaded.clear()
