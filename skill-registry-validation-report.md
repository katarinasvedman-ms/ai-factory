# Toolbox skill registry: discovery and loading validation

Date: 16 November 2026

Scope: Foundry Toolbox registry and the factory's Copilot SDK provider adapter

## Executive conclusion

**The PoC demonstrated that a consumer can discover skills in the Toolbox
registry, resolve a requested skill to its published version, and load its
instructions and supporting files through the SDK adapter.**

Existing local evidence contains 30 task-session attempts. In all 27 completed
sessions, provider operations succeeded, requested skills were materialized,
and recorded content hashes matched the pinned release. Across these sessions,
all seven published coder skills were retrieved at least once. Three attempts
failed during provider discovery: two authentication failures and one timeout.
These failures remain part of the evidence; this is a functional demonstration,
not a claim of uninterrupted service availability.

The scope is registry discovery, resolution and loading, not model skill
selection or generated-code quality.

## Scope and results

The question is: **Can the consumer find and load a published skill from the
registry?**

Here, "find" means discovering the catalog and resolving a requested skill name
and pinned version. It does not mean deciding which skill best suits a task or
evaluating semantic-search ranking.

| Capability | Acceptance criterion | Observed result |
| --- | --- | --- |
| Discover | Retrieve available skill metadata from the configured Toolbox | Seven coder skill entries discovered |
| Resolve | Locate a requested skill using the exact published binding | Requested skills resolved against pinned Toolbox/skill versions |
| Load instructions | Retrieve the requested `SKILL.md` and supply it through the SDK provider | Demonstrated across all seven coder skills |
| Load supporting files | Materialize the activated package's supporting files and make them accessible | Supporting archives materialized; .NET validation and database migration references were also read |
| Verify content | Check downloaded content against published per-file hashes | Matching pinned content recorded in all 27 completed sessions |
| Clean up | Remove session-local skill files after consumption | Cleanup recorded in all 30 attempts |

## Demonstrated mechanism

```text
Exact published release binding
  -> authenticated Foundry Toolbox catalog discovery
  -> role-permitted skill descriptors exposed to the SDK
  -> requested skill name resolved to pinned content
  -> SKILL.md and, where present, supporting archive retrieved
  -> content verified and materialized in a session-local directory
  -> instructions returned to the SDK; supporting files available
  -> temporary files removed when the provider closes
```

`ToolboxSkillProvider.list_skills` exposes metadata without loading all skill
bodies. `read_skill` retrieves the requested instructions and any supporting
archive, checks metadata/content consistency and published hashes, and returns
the instruction text to the SDK. There is no requirement for a preinstalled
local skill mirror; temporary files are created for the session.

Foundry supplies the registry and content. The factory adapter supplies
authentication, discovery/retrieval, pin checks, application allow-list
filtering and temporary-file handling. Copilot SDK supplies the native provider
integration. This separation is implemented and demonstrated for this consumer;
other runtimes would need their own adapter.

## Evidence

The report uses existing local evidence. No additional live sessions were run
and raw attempt ledgers were not rewritten.

| Evidence set | Attempts | Completed with successful provider operations | Discovery failures |
| --- | --- | --- | --- |
| Local run set 1 | 18 | 16 | 2 authentication failures |
| Local run set 2 | 12 | 11 | 1 timeout |
| Total | 30 | 27 | 3 |

Both run sets exercised the same registry/loading path.
All attempt evidence digests were checked again when preparing this report.

Recorded materializations cover the full coder catalog:

| Skill | Published skill version | Sessions materializing the skill |
| --- | --- | --- |
| `api-development` | 2 | 8 |
| `database-changes` | 2 | 4 |
| `dotnet-development` | 3 | 4 |
| `java-development` | 2 | 3 |
| `legacy-code-analysis` | 2 | 3 |
| `python-development` | 2 | 9 |
| `test-strategy` | 2 | 8 |

These are 39 skill materializations across 27 completed sessions.

The local evidence used:

- Release: `factory-skills` repository, `foundry-43842056109-1`.
- Published source: `8486e807960b51b686d06765fd9afb1f54544d23`.
- Toolbox: `coder-skills`, version **3**.
- Binding SHA-256: `3099bdc5856e561bdadd686e6487d73ec627625e62e1d62254422a921e96f032`.
- Runtime: Copilot SDK **1.0.31**, managed CLI **1.0.109**.
- Foundry authentication: existing authorized local Azure CLI credentials,
  not the publisher's historical test identities.

Local artifacts are retained under:

```text
.artifacts\skill-selection\2db6ae39-bb4a-4490-8b0e-35c21e77104a
.artifacts\skill-selection\cae2218e-45a1-4de3-a2d6-02eb0b87e8af
```

`configuration.json` records release, content and runtime pins; `discovery.json`
records catalog metadata. Per-session manifests, provider metrics, native SDK
events and resource-read events establish resolution, materialization,
content identity and cleanup. Raw artifacts contain environment metadata and
must not be published automatically.

Separate historical Azure evidence records one coder-only ACA Job execution
on 13 November using managed identity for Foundry and loading .NET and
test-strategy. That execution used Toolbox **v1**, not v3. It supports the
Azure-hosted consumption path, but must not be presented as deployed validation
of the current v3 binding or every factory role. See the deployment evidence
described in [README.md](README.md).

## Failure handling and boundaries

The three discovery failures stopped their sessions without silently falling
back to the bridge. One timeout was initially classified as `runtime-or-policy`;
its provider failure evidence identifies `TimeoutError`. The raw ledger is
preserved.

Existing deterministic provider controls separately cover metadata-only
discovery, on-demand retrieval, supporting-file materialization, rejection of
nonpermitted requests, malformed/changed content, and cleanup. These are
controlled tests, not additional production observations.

Application allow-list filtering remains necessary. Separate Toolbox endpoints
have not been established as Azure authorization boundaries; project-level
grants previously permitted cross-Toolbox access. This report does not claim
server-enforced per-Toolbox isolation, production availability, semantic-search
quality, validation of every role, or portability to a non-SDK consumer.

## Conclusion

**The registry discovery, resolution and loading objective is demonstrated for
the tested SDK/provider integration and pinned release.**
