# AI factory skills PoC

Python Copilot SDK workers with dynamically selected, reproducible skills from
GitHub or Microsoft Foundry Toolbox. Skill content lives independently in
`katarinasvedman-ms/factory-skills`.

The PoC supports architect, planner, coder, reviewer, and tester roles without
embedding skill names into agent implementations. The first demonstration uses
coder/tester sessions and a small unmodified .NET/SQL Server fixture. Workers
read snapshots and create approval-required proposals; they cannot execute
shell commands, commit, push, deploy, or modify application source.

## Architecture

```text
Trusted request categories + policy + prepared application checkout
  -> static technology profiling with file evidence
  -> role/task selection and dependency/compatibility checks
  -> approved immutable skill snapshot
  -> per-execution, per-role native skill directories
  -> manifest persisted before starting model sessions
  -> native SDK skill activation and controlled supporting-resource reads
  -> proposals, role outputs, and structured evidence
```

`SkillResolver` separates distribution from consumption. Local/GitHub and the
default Foundry bridge return a verified full snapshot for native
`skill_directories`. Experimental Foundry provider mode returns pinned discovery
metadata instead and uses the SDK's native `skill_provider`. Both use the native
`skill` tool to activate instructions; neither is a custom skill engine.

GitHub retrieval requires an approved full commit SHA. Foundry first verifies
the exact toolbox version and each immutable skill reference, discovers/reads
MCP Resources, downloads pinned full ZIP packages, and compares all bytes with
an operator-approved skills tree before configuring sessions. Generic MCP tool
support alone is not used as a substitute for skill loading.

## Local setup and demo

Requires Python 3.13, pip 25.1+, Git, and an authorized Copilot account.
The verified runtime is `github-copilot-sdk==1.0.31` with its pinned CLI
`1.0.109`. That CLI is SDK-managed; the user's interactive CLI can have a
different version. Factory clients ignore an inherited `COPILOT_CLI_PATH` so
launching from another CLI cannot silently replace the SDK-pinned runtime.

Keep the two repositories as sibling folders. From `ai-factory` in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements.lock -e . --group dev
.\.venv\Scripts\ai-factory.exe runtime-info

$execution = .\.venv\Scripts\ai-factory.exe prepare `
  --target .\tests\fixtures\orders --skills ..\factory-skills `
  --request .\examples\request.json --policy .\examples\policy.json

.\.venv\Scripts\ai-factory.exe verify $execution
.\.venv\Scripts\ai-factory.exe run $execution --model claude-haiku-4.5 --live
```

`prepare` does not call a model. `run --live` explicitly authorizes model calls
using existing Copilot authentication, or `FACTORY_COPILOT_TOKEN` if supplied
through an approved secret mechanism. Do not reuse the GitHub repository-read
token as the Copilot credential. Model availability depends on the account.

The example asks for a small input-validation proposal in `OrderService.cs`.
Inspect `manifest.json`, `events.jsonl`, `proposals`, and role result files in
the printed execution directory. Target files remain unchanged.

Use `--roles architect planner coder reviewer tester` with `prepare` to include
all five roles. Use trusted `--tasks` categories to enable API, database, or
legacy skills when appropriate. These categories and the policy are platform
configuration, not model guesses extracted from untrusted repository text.
For unknown stacks, only applicable generic skills are selected.

### GitHub source

After an approved skills commit exists:

```powershell
.\.venv\Scripts\ai-factory.exe prepare --source github `
  --github-repository katarinasvedman-ms/factory-skills `
  --github-commit APPROVED_FULL_COMMIT_SHA `
  --target .\tests\fixtures\orders `
  --request .\examples\request.json --policy .\examples\policy.json
```

Use a short-lived, repository-scoped GitHub App installation token through
`GH_TOKEN`; existing `gh` authentication is a local-development fallback.
Azure managed identity does not directly grant GitHub access. Cross-host
archive redirects never receive the GitHub authorization header.

### Foundry source

Publish approved skills using the sibling repository's explicit publisher.
Supply its resulting binding and the same approved skill tree:

```powershell
.\.venv\Scripts\ai-factory.exe prepare --source foundry `
  --skills ..\factory-skills `
  --foundry-binding ..\factory-skills\.artifacts\foundry-binding.json `
  --target .\tests\fixtures\orders `
  --request .\examples\request.json --policy .\examples\policy.json
```

The resolver uses `DefaultAzureCredential` and token scope
`https://ai.azure.com/.default`. Managed identity is appropriate in ACA;
`AZURE_CLIENT_ID` must be the user-assigned identity's client ID, not its name.
For an existing Azure CLI login, `AZURE_TOKEN_CREDENTIALS=AzureCliCredential`
can explicitly select that credential. Tokens are never written to manifests.
The initial endpoint validator supports Azure public cloud only.

### Experimental on-demand Toolbox provider

The bridge remains the default. Opt into the provider without `--skills`:

```powershell
.\.venv\Scripts\ai-factory.exe prepare --source foundry --skill-mode provider `
  --foundry-binding ..\factory-skills\.artifacts\foundry-binding.json `
  --target .\tests\fixtures\orders `
  --request .\examples\request.json --policy .\examples\provider-policy.json
```

The binding can live anywhere; provider mode needs only `project_endpoint`,
`toolbox`, and numeric `version`, not the skills checkout or an approved mirror.
The policy explicitly supplies `permitted_skills` per role. Discovery reads
only metadata; SKILL.md and any whole-folder supporting archive are read through
the pinned MCP endpoint only when the SDK requests that skill. Supporting files
exist in a private session temporary directory that is removed on disconnect.
The SDK descriptor includes an internal absolute `directory` pointer in addition
to name/description; it does not expose instructions in the catalog.

Permission handling and available tools are unchanged: materialized scripts
cannot be executed. Provider errors fail the run, including when the SDK hides
the underlying error from the model. There is no automatic switch to bridge
on an activation failure. To use the bridge, explicitly prepare another
execution with `--skill-mode bridge` and its approved mirror.

For jobs, use `"skill_mode": "provider"` in the Foundry `FACTORY_SOURCE` object
and omit `approved_skills`. Provider resumes retain manifest pins but need the
Toolbox for activations in unfinished sessions; unlike bridge mode, they do not
have a persistent local package snapshot.

See [the spike report](spike-report.md) for verified API shapes, the real
coder/tester comparison, native events, fault evidence, and remaining risks.

Role-specific bindings can instead supply a `roles` map with one pinned
`<role>-skills` toolbox and Entra identity per role. Each provider receives only
its selected binding; token principal/client/tenant mismatches are rejected
before requests. Keep `permitted_skills` in the policy. Local role workers run
in separate processes with their own certificate credential environment.
`aiohttp` is required for these async Azure credentials and is locked.

See [the role-isolation report](role-isolation-report.md): distinct identities
and endpoint configuration were verified, but a working toolbox-scoped RBAC
boundary was **not** established. Project-level Foundry User grants allow
cross-toolbox access and must not be described as role authorization isolation.

The opt-in provisioning utility `tools\setup_role_isolation.py` requires explicit
`--project-endpoint`, `--project-scope` (the project's full ARM ID), and
`--tenant-id`, alongside its catalog, output, credentials and `--apply` arguments.
It creates identities and role grants; it is not part of ordinary setup or CI.
Keep its output in `.artifacts` and certificate credentials outside the checkout.
The RBAC probe utilities read the same environment configuration from that saved
state rather than targeting a hard-coded Azure environment.

## Consistency, security, and failure handling

The persisted manifest records execution ID, source repository/commit or
Foundry versions, target commit when available, target content digest,
profiling evidence, roles/tasks, assignments, per-file skill digests, request,
policy, and SDK/CLI versions. A non-Git fixture is explicitly marked as such;
it has a content identity, not an invented commit.

Run the same execution directory to resume. No upstream resolver is needed;
target and role packages are reverified in bridge mode. Provider mode verifies
persisted metadata and pins, then re-fetches activated content for unfinished
sessions, checking previously observed file digests when available. Runtime/model
configuration cannot silently change on resume.
Different executions use UUID directories. An exclusive worker lock prevents
simultaneous runs of the same execution; after an abnormal termination, an
operator must establish that no worker is active before removing its stale lock.

Repository content is untrusted data. Native instruction/config/hook/plugin
discovery is disabled, worker capabilities are explicitly enumerated, native
permission requests default to denial, and paths/resources are checked against
the approved manifest. Tools require an observed approved native skill activation
before doing work. Skill guidance itself is not a security boundary.

Archives have path/link/collision and size limits. Transient distribution errors
have bounded retries; unauthorized/missing/tampered required content fails
closed. No fallback to `latest` exists. The PoC limits target snapshots to
2,048 files/20 MiB and individual files to 1 MiB; large enterprise repositories
need a separately designed bounded checkout/profiling strategy.

Only the platform/operator may change policy, runtime code, or execution storage.
Digests detect changes relative to trusted metadata; they are not a defense
against an administrator replacing both metadata and content.

## Checks and live evaluations

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests

$env:FACTORY_LIVE = '1'
$env:FACTORY_SKILLS_ROOT = (Resolve-Path ..\factory-skills).Path
$env:FACTORY_MODEL = 'claude-haiku-4.5'
.\.venv\Scripts\python.exe -m pytest .\tests\test_live_demo.py -q
```

Offline checks cover profiling, role selection, dependencies, compatibility,
integrity, release changes, resume, transport errors, safe extraction, role/tool
permissions, and both distribution protocols. The opt-in live evaluation checks
native activation events, supporting-resource access, observable compatibility
details, proposal creation, and non-activation of irrelevant available skills.
Model behavior is probabilistic; one passing run is not a production quality
guarantee.

The tester currently inspects proposals and produces a test strategy; it does
not run application builds/tests. Result artifacts explicitly mark these NOT
RUN. A separately authorized, credential-free isolated check runner is needed
before claiming that generated application code passes compilation or tests.
There is no SQL Server connection or real ServiceNow integration in this PoC.

## Container Apps Jobs

`Dockerfile` installs locked dependencies, runs as a non-root user, and prefetches
the SDK-pinned CLI so fresh workers do not download it during job startup.
The prefetch helper is a private SDK API deliberately tied to the exact package
pin; review it when updating SDK versions.

```powershell
docker build --tag ai-factory:poc .
docker run --rm --entrypoint ai-factory ai-factory:poc runtime-info
az bicep build --file .\infra\main.bicep --stdout
```

`infra\main.bicep` describes a manually triggered job using an existing ACA
environment, an existing user-assigned identity, Key Vault secret references,
an approved ACR image digest, and an existing Azure Files mount for input
snapshots and durable execution artifacts. It does not create credentials or
grant roles. Configure ACR pull, Key Vault secret access, project authorization,
storage permissions for UID 10001, networking, and logging retention separately.
Do not put secrets into ordinary Bicep parameters or request JSON.

`ai_factory.job` prepares an execution from trusted `FACTORY_SOURCE`,
`FACTORY_POLICY`, `FACTORY_REQUEST`, `FACTORY_TARGET`, and
`FACTORY_ARTIFACT_ROOT`. Optional `FACTORY_ROLES`/`FACTORY_TASKS` are JSON arrays.
`FACTORY_LIVE=1` plus `FACTORY_MODEL` enables SDK work. For a retry, set
`FACTORY_RESUME_EXECUTION` to the persisted UUID; it reuses pinned files without
requiring current source configuration. The template disables automatic job
retries so retries cannot accidentally create unrelated new executions.

No ACA deployment was performed. The template compiles. Local image validation
is currently blocked by Docker's TLS handshake failures downloading Python
packages from `files.pythonhosted.org`; TLS verification was not disabled.
Fix the container build network/trust configuration before deploying.

## Verified PoC state

On 2026-11-12, local native SDK evaluation passed, and real Foundry publication,
MCP skill discovery, pinned package downloads, and coder/tester consumption
completed. The Foundry-backed execution is retained at
`.artifacts\executions\649a473a-9bc3-4176-be92-c7954331b3c9`.
The successful opt-in evaluation is under `.artifacts\live-evaluation-02`.
These artifacts include actual activation/resource/proposal evidence, not just
an agent's claim to have used skills.

The on-demand provider also completed the same real coder/tester scenario.
Native events report `source: sdk`, rather than the bridge's `source: custom`.
Its session temporary folders were removed afterward. The spike is opt-in:
it fetched fewer distinct skills but more bytes in this small catalog, and
introduces an activation-time dependency on the preview Toolbox service.

GitHub retrieval has transport/contract tests, but a live skills-repository
download awaits the first approved commit: both remote repositories were empty
at the start, and no commits/pushes were performed. CI workflows are provided
but have not yet run remotely.

GitHub keeps initial infrastructure minimal. Foundry adds managed Azure
distribution with preview protocol dependencies. OCI/ACR would suit larger,
signed/replicated bundles; Blob would suit simple managed-identity downloads.
Neither alternative is implemented, and neither requires changes to individual
roles when added behind the resolver.
