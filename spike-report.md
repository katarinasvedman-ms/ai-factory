# Spike: on-demand Toolbox skill provider

## Result and recommendation

**The native provider works with the pinned Python Copilot SDK. Keep it opt-in;
not yet the default.** The same coder/tester scenario completed in both modes.
Provider mode needed no approved local skills mirror, fetched only the two
skills actually activated, and removed its per-session supporting directories.
Native SDK events identify provider activations as `source: sdk`, not file-backed
`source: custom`.

The bridge remains unchanged as an explicit fallback. Do not silently switch
implementations during an activation failure. Provider mode reduced startup to
the first ready session in this sample, but fetched **more**, not fewer, bytes
and had a slightly longer complete execution. Experimental SDK APIs, preview
Toolbox APIs, repeated metadata discovery and activation-time availability
remain material adoption risks.

No commits, pushes, deployments, Azure resource creation, Toolbox changes,
skills-repository edits, or application-source changes were performed for this
spike. Proposals are approval-required artifacts, not applied changes.

## Prerequisites: verified before implementation

### Copilot SDK

| Component | Verified value |
| --- | --- |
| Python distribution | `github-copilot-sdk==1.0.31` |
| SDK-managed CLI, checked at runtime | `1.0.109` |
| Official source tag | `github/copilot-sdk`, `v1.0.31` |
| Tagged `python/copilot/session.py` Git blob | `453e9b9a928417b751376ad8a58298bb1dc4f6da` |
| Installed source matches tagged source byte-for-byte | Yes |
| Installed source SHA-256 | `2ea68a4b17e1e3112881426c81fb717368eb335a8c91a8bf884f14b7e03af213` |

Official references:

- [SDK skills documentation](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/skills),
  including provider content, supporting files, limits and errors.
- [Pinned Python provider source](https://github.com/github/copilot-sdk/blob/v1.0.31/python/copilot/session.py).
- [Pinned generated descriptor/RPC source](https://github.com/github/copilot-sdk/blob/v1.0.31/python/copilot/generated/rpc.py).

The actual Python API is:

```python
CopilotClient.create_session(
    ...,
    skill_provider: SkillProvider | None = None,
    skill_directories: list[str] | None = None,
)

SkillProvider.list_skills(self)
# list[SkillProviderDescriptor] | None, synchronously or awaitably

SkillProvider.read_skill(self, name: str)
# str | None, synchronously or awaitably

SkillProviderDescriptor(
    description: str,
    name: str,
    argument_hint: str | None = None,
    disable_model_invocation: bool | None = None,
    user_invocable: bool | None = None,
    directory: str | None = None,
)
```

These are **experimental**, session-scoped APIs. Providers must be supplied again
for resumed sessions and must handle concurrent calls. Async callbacks receive
`asyncio.CancelledError` when cancelled; the runtime allows 30 seconds per call.
Catalogs are limited to 1,024 skills/1 MiB metadata; descriptions and argument
hints to 1,024 characters; instruction bodies to 1 MiB UTF-8.

Included frontmatter metadata must match the catalog. Without `directory`, a
provider has no base directory and cannot support ordinary relative references.
The descriptor therefore includes an internal absolute per-skill directory
pointer **in addition to semantic name/description metadata**. The SDK does not
display this pointer in its catalog, and no instructions/supporting bytes are
returned by `list_skills`.

The SDK lists that directory after `read_skill` returns, permitting lazy
creation. Missing/unlistable directories do not themselves fail activation.
The application must verify materialization and own cleanup. Permission checks
still apply; the documentation's blanket-approval examples were not adopted.
After compaction the base-directory pointer may require reactivation.

The pinned SDK deliberately sanitizes callback exceptions to JSON-RPC `-32603`,
such as `Skill provider readSkill failed`. Consequently, the application records
the detailed failure itself, prevents subsequent worker actions, and refuses a
successful role result even if the model continues after the generic SDK error.

### Actual Toolbox MCP responses

Existing project (environment-specific names redacted for publication):
`https://<foundry-resource>.services.ai.azure.com/api/projects/<project>`

Existing immutable binding: Toolbox `factory-skills`, version `"1"`; ten pinned
skill references, each service version `"1"`. These service versions are not the
catalog's semantic skill versions.

Endpoint:
`{project}/toolboxes/factory-skills/versions/1/mcp?api-version=v1`

Authentication used `DefaultAzureCredential`, selecting the existing Azure CLI
credential through `AZURE_TOKEN_CREDENTIALS=AzureCliCredential`. Every HTTP
request obtains a token for `https://ai.azure.com/.default` and sends
`Foundry-Features: Skills=V1Preview`. Tokens/authorization headers were not saved.

Observed initialization returned HTTP 200 and:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-11-25",
    "capabilities": {
      "tools": {},
      "tasks": {
        "list": {},
        "cancel": {},
        "requests": {"tools": {"call": {}}}
      },
      "resources": {"listChanged": false, "subscribe": false},
      "extensions": {"io.modelcontextprotocol/skills": {}}
    },
    "serverInfo": {"name": "Foundry Toolbox MCP", "version": "1.0.0"}
  }
}
```

The client offered `2025-03-26`; subsequent requests used the actual negotiated
`2025-11-25` in `MCP-Protocol-Version`. Initialization notification returned 204.
Session headers are retained in memory, not persisted as evidence.

`resources/list` returned 13 resources in one page, without `nextCursor`:
ten entry documents, two supporting-folder archives and one discovery index.
The relevant actual entry, excerpted from that response, was:

```json
{
  "uri": "skill://dotnet-development/SKILL.md",
  "name": "dotnet-development",
  "description": "Entry document for skill 'dotnet-development'. Read first; lists supporting files via the skill://dotnet-development/{path} template.",
  "mimeType": "text/markdown"
}
```

**This is transport guidance, not the skill's frontmatter description.** Using
it as the SDK descriptor would conflict with the original SKILL.md. The advertised
`skill://index.json` solves that without reading any unactivated instructions.
`resources/read` returns it as JSON text inside `result.contents[0].text`.
The decoded index uses schema
`https://schemas.agentskills.io/discovery/0.2.0/schema.json`; its .NET entry is:

```json
{
  "name": "dotnet-development",
  "type": "archive",
  "description": "Implement, inspect, review, or test C# and .NET changes using the repository's target framework and conventions. Use when project files establish .NET, not for Java or Python tasks.",
  "url": "skill://dotnet-development.zip"
}
```

Single-file entries instead use `"type": "skill-md"` and their SKILL.md URI.
The index is discovery metadata, not skill-body preloading.

`resources/templates/list` returned:

```json
{
  "resourceTemplates": [
    {
      "uriTemplate": "skill://dotnet-development/{path}",
      "name": "dotnet-development-files",
      "description": "Files belonging to skill 'dotnet-development'. Substitute {path} with a file path discovered in skill://dotnet-development/SKILL.md."
    },
    {
      "uriTemplate": "skill://database-changes/{path}",
      "name": "database-changes-files",
      "description": "Files belonging to skill 'database-changes'. Substitute {path} with a file path discovered in skill://database-changes/SKILL.md."
    }
  ]
}
```

Both supporting files were **individually addressable** and returned HTTP 200:

- `skill://dotnet-development/references/validation.md`
- `skill://database-changes/references/migrations.md`

Their response shape was the following, with the shown text abbreviated:

```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": {
    "contents": [{
      "uri": "skill://dotnet-development/references/validation.md",
      "mimeType": "text/markdown",
      "text": "# .NET validation checklist\n\n..."
    }]
  }
}
```

SKILL.md has the same `contents`/`text` shape, including original YAML
frontmatter. Archive resources use base64 `blob` content, successfully decoded
and extracted during the native provider run.

**Both prerequisites passed.** Full observed prerequisite responses and source
verification are retained under `.artifacts\toolbox-provider-spike` as
`prerequisites.json`, `resource-templates.json`, and `sdk-api.json`.

## Implementation and code paths

| Concern | Actual implementation |
| --- | --- |
| Resolver interface | `resolvers.SkillResolver.resolve` supports full `Snapshot` or metadata-only `ProviderSnapshot` |
| Metadata-only preparation | `provider.ToolboxProviderResolver._resolve` -> `ToolboxClient.catalog` -> pinned version GET, MCP initialization, paginated `resources/list`, index read |
| Role selection | `ProviderSnapshot.assignments` validates trusted `policy.permitted_skills` against `allowed_skills` and discovered pins; `execution.prepare` persists exactly those role assignments |
| SDK catalog | `ToolboxSkillProvider.list_skills` rechecks pinned metadata and returns only the role's name/description descriptors plus internal directory pointers |
| Activation | Native SDK `skill` -> `ToolboxSkillProvider.read_skill` -> `resources/read` for that skill's original SKILL.md |
| Supporting files | Only for an activated multi-file skill, read its advertised MCP ZIP; safely extract its entire folder into that session's private directory |
| SDK loading | `runtime._run` passes `skill_provider=provider` and `skill_directories=[]`; bridge keeps its existing directory configuration |
| Permission policy | Existing `WorkerTools.permission` and available-tool names are unchanged; resource reads still validate role, safe path and observed file hash |
| Activation evidence | Native `skill.invoked` / `skill.invoked.ref` callbacks; unmodified `notification.to_dict()` retained in `native-skill-events.jsonl` |
| Manifest updates | Atomic writes record observed file hashes on materialization and native role/skill activations afterward |
| Cleanup | `runtime._session` disconnects the SDK before closing the provider and removing its temporary directory, including failure paths |

Using an activated-folder archive is intentional: it supplies a complete
inventory, including scripts/assets not linked from Markdown. The provider does
not guess a complete package from Markdown links. It does **not** download all
archives, use a local approval mirror, or fall back to the bridge. Individual
support-file addressability was separately verified above; this implementation
materializes the activated folder as a unit, not one file per subsequent read.

The supporting archive's SKILL.md must equal the individually read instructions.
Unsafe archive paths, links, collisions and oversized packages fail closed.
Repeated activations can reuse verified session-local instructions, without
keeping an unbounded second instruction cache. Previously observed manifest
hashes must match on a later role/session fetch.

The in-memory resource cache is an LRU bounded by **32 entries and 4 MiB**.
HTTP reads and extracted session resources are bounded by **20 MiB**, with at
most **2,048 materialized files**. Calls are async and serialized per provider.
HTTP timeouts are six seconds; transient transport/429/5xx failures receive at
most three attempts with 0.25/0.5-second backoff. A whole provider callback has a
24-second deadline, below the SDK's 30-second ceiling. Cancellation stops async
I/O; no background download threads continue after cancellation.

Token acquisition occurs per HTTP attempt, allowing the Azure credential to
refresh expired tokens rather than retaining a session-long bearer string.
Unauthorized requests fail instead of being silently approved or retried
indefinitely.

### Configuration and selection boundary

`prepare --source foundry --skill-mode provider` selects the new implementation;
`--skill-mode bridge` remains the default. Provider mode needs no `--skills`.
`examples\provider-policy.json` gives the same effective coder/tester skill sets
as the original scenario. Jobs select the same mode through
`FACTORY_SOURCE.skill_mode`.

The discovery index does **not** include the factory catalog's technology,
role, dependency or compatibility metadata. Provider mode therefore requires an
explicit operator-supplied per-role `permitted_skills` map. It does not invent
those classifications, read every SKILL.md to reconstruct them, or quietly
preserve the full-mirror requirement. Static target profiling remains available
as worker context. The bridge's existing automatic catalog selection and
dependency checks remain intact.

After session cleanup, provider mode retains discovery metadata, manifests,
hashes, native-event evidence and approval artifacts, not persistent role
package directories. Resuming unfinished roles requires Toolbox availability
again. Bridge executions retain their pinned full local package snapshots.

## Measured coder/tester comparison

Same request, target fixture, model `claude-haiku-4.5`, SDK/CLI pins and effective
role assignments. Measurements were taken on 2026-11-12, provider first, then
bridge, on the same workstation. This is one non-randomized sample, not a
performance benchmark or quality guarantee.

| Metric | Bridge | Provider |
| --- | --- | --- |
| Startup time to first session ready | **83.06 s** | **61.53 s** |
| SDK session-ready time, coder / tester | 1.22 s / 0.81 s | 20.08 s / 21.42 s |
| Bytes fetched from Toolbox distribution | **24,485 B** | **41,204 B** |
| Skills fetched vs skills activated | 10 distinct fetched, 2 distinct activated; 10 SKILL.md reads | 2 distinct fetched, 2 distinct activated; 3 session-scoped SKILL.md reads |
| Native SDK skill activation events observed | 3 `skill.invoked`, `source: custom` | 3 `skill.invoked`, `source: sdk`, empty `path` |
| Supporting-resource reads observed | Coder and tester each read `.NET references/validation.md` | Same two reads; each role materialized only its activated .NET folder |
| Toolbox unreachable at startup | Preparation fails; transport-boundary test | Real SDK controlled-fault probe refuses session/run; no successful role result |
| Toolbox unreachable mid-session | Prepared skills remain usable; comparison closes Toolbox transport before SDK execution | Real SDK controlled-fault probe fails activation/run; no native activation or proposal; already materialized content is locally reusable until a new activation fails |
| Non-permitted skill exists in Toolbox | Java is fetched by the bridge, but absent from both native role catalogs | Java is advertised remotely but absent from both native role catalogs; its instructions/archive are never fetched |
| Complete execution time | 132.33 s | 145.04 s |
| Approved local skills mirror required | Yes | No |

Startup begins before resolution/preparation and ends after the first real
SDK session's native catalog has been checked with `session.rpc.skills.list`.
It includes authentication and SDK process startup. Per-role SDK timings start
at `create_session` and include that catalog check. The catalog check causes
additional provider `resources/list` calls; these are included in measurements.

Bytes are decoded HTTP response-body bytes, excluding headers, token-service
traffic and TLS overhead. Bridge totals include both Toolbox MCP responses
and the pinned skill-content REST downloads selected by that Toolbox binding;
provider totals include its metadata and MCP JSON/base64 archive responses.
Primary comparison calls succeeded without application retries. This is not a
packet-level network-bandwidth measurement.

The tiny catalog's instructions compress well in the bridge's ZIP downloads.
Repeated per-role metadata and MCP archive encoding outweighed avoiding eight
unactivated skills here. Do not extrapolate lower byte use or faster complete
execution from the provider's fewer distinct skill fetches.

### Native activation evidence

Provider execution:
`79174714-1c23-4d05-83b8-922aacbbec00`

Bridge execution:
`34125fbd-0730-441d-8675-9d88a964c50c`

Both reside beneath
`.artifacts\toolbox-provider-spike\comparison\<mode>\<execution-id>`.
`comparison.json` contains the measurements and per-request body-byte counts.
Each execution retains role catalogs, original native skill events, role
results and proposals. Provider role metrics record cleanup success.

| Mode / role / skill | Native event ID | UTC |
| --- | --- | --- |
| Provider coder .NET | `6eb747c2-f232-4d28-8fe8-382dd9a8c9d4` | 16:12:37.791 |
| Provider tester .NET | `de1f45a3-fc40-4c65-b52d-eb9c1f321953` | 16:13:23.624 |
| Provider tester test-strategy | `01d21f8f-4cbe-4c75-8178-500de2c7f537` | 16:13:27.116 |
| Bridge coder .NET | `a0410a92-2451-49de-8199-f8ac3ce8e301` | 16:15:17.352 |
| Bridge tester .NET | `9f6354d0-52de-405c-8f82-1054f0b9fded` | 16:15:36.836 |
| Bridge tester test-strategy | `59f11c1a-fbac-4bad-8793-32afe8873174` | 16:15:36.845 |

These are SDK events, not statements in model output. Provider event data
includes original loaded content, `source: sdk`, `path: ""` and
`trigger: agent-invoked`. Raw events remain ordinary application-owned evidence,
not signed attestations.

Both target snapshot identities remained unchanged. Coder produced an
approval-required proposal; tester inspected it and returned recommendations.
Application builds/tests remain **NOT RUN** because no executor is authorized.

### Failure evidence

No real Azure outage was induced. Controlled faults were injected at the
Toolbox request boundary while retaining the real SDK/CLI:

- Startup probe: `056be1b4-9f99-4702-b868-a870f7e1a102`.
- Mid-session activation probe: `1990a051-6e8f-473b-b1f3-87e6dbd62c39`.

Both are under
`.artifacts\toolbox-provider-spike\native-failure-probes`.
`failures.json` retains the application error and native events; each probe
also has its own manifest, event log and provider cleanup metrics.

The startup probe failed native catalog loading. The mid-session probe let
metadata load, then made activation content reads unavailable. Native skill
tool calls failed; no `skill.invoked` was accepted, no proposal or successful
role result was written, and both temporary folders were removed. The model
could not convert a failed activation into application success.

Bridge startup failure is separately covered by a transport-boundary test.
The real bridge scenario ran after its distribution HTTP client and credential
were explicitly closed: it has no activation-time Toolbox retrieval path.
That is evidence of disconnected package use, not a claim that Azure was
actually unavailable.

## Tests and reproduction

Focused tests cover metadata-only discovery, explicit per-role selection,
non-permitted reads, original frontmatter, archive consistency, unlinked script
materialization without execution, safe extraction, bounded cache, token
reacquisition, transient retries, deadlines, cancellation, fatal activation
errors, unchanged permissions, native-event bookkeeping, cleanup and target
immutability. Existing bridge/runtime/execution tests remain in place.

Final offline result: **63 passed, 1 opt-in live test skipped**. Ruff lint and
format checks passed. The two real comparison executions and the real SDK
fault probes were run separately from that offline suite.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests tools
.\.venv\Scripts\python.exe -m ruff format --check src tests tools

$env:AZURE_TOKEN_CREDENTIALS = 'AzureCliCredential'
.\.venv\Scripts\python.exe .\tools\compare_skill_modes.py `
  --binding ..\factory-skills\.artifacts\foundry-binding.json `
  --skills ..\factory-skills --modes provider bridge `
  --model claude-haiku-4.5 --live
```

The comparison command's `--skills` parameter is used **only by bridge mode**.
Its provider branch never constructs a `LocalResolver`. For provider-only
operation, use the normal CLI command in the README without `--skills`.
That exact mirror-free CLI preparation and verification also succeeded:
execution `4434b641-2e9e-4691-ab39-6ec95500d7f9`, under
`.artifacts\toolbox-provider-spike\cli`, has no local `skills` directory.

The retained XML test report is
`.artifacts\toolbox-provider-spike\tests.xml`.
The standalone fault probe can be repeated against a prepared provider
execution using `tools\probe_provider_failures.py --execution ... --output ...
--model claude-haiku-4.5 --live`.

## Open risks and adoption decision

1. SDK provider/supporting-directory APIs are experimental, and Foundry Skills
   requires a preview feature header. Recheck both before changing either pin.
2. The provider depends on Toolbox and Azure authentication during new
   activations and unfinished-session resumes. The bridge does not. Local
   caching is bounded and is not an offline-distribution guarantee.
3. Provider resolution plus execution preparation took 23.41 seconds; native
   session/catalog readiness added 20-21 seconds per role. The 24-second
   per-callback deadline can reject slow or retrying discovery. Investigate
   credential/process latency and repeated metadata reads before production.
4. This index does not publish factory selection/dependency metadata or optional
   SDK invocation flags. Explicit role policy is required; unsupported
   invocation frontmatter is rejected on activation rather than guessed.
5. Multi-file activation currently downloads the activated folder archive.
   This proves per-activated-skill laziness, not per-supporting-file network
   laziness. Scripts are materialized but remain non-executable under this
   proposal-only tool policy.
6. First-fetch trust is the approved Toolbox version and manifest allowlist,
   not a local content mirror or signed package attestation. Observed hashes
   detect subsequent byte changes; execution storage must remain operator-owned.
7. A single successful scenario and controlled faults do not establish
   long-session token-expiry, compaction, service reliability or load behavior.
   Token refresh/cancellation contracts have focused tests, not a multi-hour soak.

**Recommendation: not yet.** Retain bridge as the default and provider as an
explicit experimental option. The architectural feasibility is verified;
promoting the provider requires acceptance of its trust/availability trade-off,
stronger selection metadata and repeatable performance/long-session evidence.
Nothing in this spike requires ACR, Blob distribution or new Azure resources.
