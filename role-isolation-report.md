# Per-role identity and Toolbox isolation

**[Verified by execution]** Five role toolboxes and five distinct local-test
service principals were created in the existing project. Coder/tester SDK
sessions used their own identities and endpoints successfully.
**Toolbox-level authorization isolation was not established.** With the requested
project-level grants, coder can also access tester's Toolbox.

Labels: **[V]** = verified by executed commands, HTTP calls, native SDK events,
or executed source inspection; **[A]** = assumption or unverified interpretation.
Environment-specific identifiers and resource names are redacted in this public
report; exact values remain only in the local, ignored evidence artifacts.

## (a) Authentication before and after

| Status | Adapter / stage | Authentication |
| --- | --- | --- |
| [V] | Before: bridge | Synchronous `azure.identity.DefaultAzureCredential` -> `AzureCliCredential`; Entra bearer token, not a key |
| [V] | Before: provider | Async `azure.identity.aio.DefaultAzureCredential` -> `AzureCliCredential`; Entra bearer token, not a key |
| [V] | Before: actual identity | Operator Entra user (object ID redacted); Azure CLI application `04b07795-8ddb-461a-bbee-02f9e1bf7b46`; token `idtyp: user` |
| [V] | After: role provider sessions | `DefaultAzureCredential` -> `EnvironmentCredential` -> certificate credential, with a different service principal per role; no client secrets or service keys |
| [V] | After: bridge / provisioning | Bridge auth is unchanged. Administrative provisioning/discovery used the existing Azure CLI user, not the worker principals |
| [V] | Audience, both modes | Requested scope `https://ai.azure.com/.default`; observed token audience `https://ai.azure.com` |
| [V] | Tenant | `<tenant-id>` |

**[V]** No key-to-Entra migration was needed. Runtime token client/principal/
tenant claims were checked against the selected role binding before HTTP calls.
These checks prevent accidental credential fallback; Azure validates token
signatures. `permitted_skills`, default-deny permissions and the proposal-only
worker tools remain in place. The missing async certificate transport was
resolved by adding and locking `aiohttp`.

## (b) Toolbox layout and pins

**[V]** All toolboxes are in the same existing project under resource
`<foundry-resource>`, resource group `<resource-group>`, subscription
`<subscription-id>`. No project was created.

**[V]** Every skill reference below is pinned to service version **`"1"`**,
exactly as in `factory-skills` v1; no skill package was republished.
`Tech` means `dotnet-development`, `java-development`, `python-development`,
`api-development`, `database-changes`.

| Status | Toolbox / version | Skill set |
| --- | --- | --- |
| [V] | `coder-skills` / `1` | Tech + `test-strategy`, `legacy-code-analysis` |
| [V] | `tester-skills` / `1` | Tech + `test-strategy` |
| [V] | `architect-skills` / `1` | Tech + `architecture-analysis`, `legacy-code-analysis` |
| [V] | `planner-skills` / `1` | Tech + `implementation-planning`, `legacy-code-analysis` |
| [V] | `reviewer-skills` / `1` | Tech + `code-review`, `legacy-code-analysis` |

**[V]** Planner and reviewer already exist in the factory. Assignments follow
the existing catalog's roles; shared skills retain the same pin in every
applicable toolbox. The old immutable `factory-skills` v1 was retained for
historical runs and the existing bridge fallback, not deleted.

**[V] Versioned MCP endpoint shapes (resource and project names redacted):**

```text
https://<foundry-resource>.services.ai.azure.com/api/projects/<project>/toolboxes/coder-skills/versions/1/mcp?api-version=v1
https://<foundry-resource>.services.ai.azure.com/api/projects/<project>/toolboxes/tester-skills/versions/1/mcp?api-version=v1
https://<foundry-resource>.services.ai.azure.com/api/projects/<project>/toolboxes/architect-skills/versions/1/mcp?api-version=v1
https://<foundry-resource>.services.ai.azure.com/api/projects/<project>/toolboxes/planner-skills/versions/1/mcp?api-version=v1
https://<foundry-resource>.services.ai.azure.com/api/projects/<project>/toolboxes/reviewer-skills/versions/1/mcp?api-version=v1
```

## (c) Identity-to-Toolbox mapping

**[V]** The permitted local-testing alternative was used: certificate-authenticated
service principals, not newly deployed ACA Jobs or user-assigned managed
identities. Each received **Foundry User**
(`53ca6127-db72-4b80-b1b0-d745d6d5456d`) at the existing project's ARM scope.

| Status / role / toolbox | Application/client ID | Service-principal object ID |
| --- | --- | --- |
| [V] architect / `architect-skills` | `<architect-client-id>` | `<architect-principal-id>` |
| [V] coder / `coder-skills` | `<coder-client-id>` | `<coder-principal-id>` |
| [V] planner / `planner-skills` | `<planner-client-id>` | `<planner-principal-id>` |
| [V] reviewer / `reviewer-skills` | `<reviewer-client-id>` | `<reviewer-principal-id>` |
| [V] tester / `tester-skills` | `<tester-client-id>` | `<tester-principal-id>` |

**[V]** The recorded execution ran coder and tester in
separate processes with role-specific certificate environments. Provider HTTP
metrics record the expected distinct principal IDs, audience and only that
role's toolbox. The shared manifest's coder/tester assignments still permit
only `.NET` and test-strategy for these scenario roles.

| Status / role | Native SDK `skill.invoked` event | Activated skill |
| --- | --- | --- |
| [V] coder | `<coder-event-id>` | `dotnet-development` |
| [V] tester | `<tester-strategy-event-id>` | `test-strategy` |
| [V] tester | `<tester-dotnet-event-id>` | `dotnet-development` |

**[V]** Events have native `source: sdk`. Supporting-resource reads occurred,
temporary skill directories were removed, proposals require approval, and
application source remained unchanged. This proves configured consumption,
not a server-enforced prohibition on using another endpoint.

## (d) Toolbox-scoped RBAC finding

**[V] Attempted scope shape:**

```text
/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<foundry-resource>/projects/<project>/toolboxes/coder-skills
```

**[V]** A Foundry User role-assignment PUT at that scope was **accepted: HTTP
201**, assignment ID `<assignment-id>`.
The assignment URL appended:

```text
/providers/Microsoft.Authorization/roleAssignments/<assignment-id>?api-version=2022-04-01
```

**[V]** GET of the toolbox ARM resource path with `api-version=2025-06-01`
returned **HTTP 400, `UnsupportedAction`**, message:
`The requested action 'toolboxes/coder-skills' is not supported`.
Assignment acceptance is therefore not evidence of a usable Toolbox ARM API
or an enforced data-plane authorization boundary.

| Status / executed test | Coder toolbox `tools/list` | Tester toolbox `tools/list` |
| --- | --- | --- |
| [V] Coder principal, requested project grant present | 200 | **200** |
| [V] Coder project grant temporarily removed; only experiment's toolbox grant remains, four probes over about one minute | 200 | **200** |
| [V] Fresh disposable principal, never granted project access, no grants | 403 | 403 |
| [V] Same disposable principal, coder-toolbox-only grant | **403** | 403 |
| [V] Coder project grant restored; temporary toolbox grant removed | 200 | **200** |

**[V]** The fresh-principal probes also checked `resources/read` for .NET
instructions: 403 on both endpoints, before and immediately after the scoped
grant. The desired **own 200 / cross 403** condition was not observed.
Only this PoC's coder project grant was temporarily removed; it was restored.
Temporary scoped assignments and the disposable control application/principal
were removed.

**[A]** Role-assignment propagation and authorization caching can affect the
short post-change observations; this experiment does not prove that no future
service version or longer propagation window can support toolbox scopes.
**[V]** In the required final configuration, project-level Foundry User allows
cross-toolbox calls. Treat the per-toolbox boundary as **configuration and
defense in depth, not verified authorization isolation**.

## (e) Privileged skills and credential handling

**[V: executed inspection] Privileged-skill list: none.** All ten SKILL.md files
are procedural guidance; none grants deployment, secret access, production-data
access or arbitrary filesystem writes. They do not declare tool grants.
`database-changes` explicitly forbids executing migrations or connecting to a
database without an authorized tool.

**[V]** The coder's existing `propose_file` capability writes bounded,
approval-required artifacts outside the application checkout. This is a
factory role tool, not a capability granted by a skill; it cannot apply changes
or arbitrarily write outside the artifact store.

**[V]** Foundry User itself is broader than skill-read access: its executed role
definition includes broad Cognitive Services data actions and project connection
secret-listing actions. Separate identities do not make those project grants
least-privilege or separate-project equivalents.
**[A]** If future skills receive production database, deployment, secret or
arbitrary-write tools, those workloads are candidates for a separately isolated
project. No such project was created here.

**[V]** Test private keys were stored under a current-user/SYSTEM-only ACL
outside both repositories, then removed after verification. No tokens,
passwords or private keys were written to source or the report. The five
service principals remain; future local runs need a newly provisioned
certificate/credential. ACA identity attachment/deployment was not performed.

**[V] Evidence:** `.artifacts\role-isolation\state.json`, `role-bindings.json`,
`sessions.json`, `toolbox-scope-probe.json`, `rbac-access.json`,
`fresh-scope-control.json`, `final-state.json`, and the execution's unmodified
`native-skill-events.jsonl` and per-role HTTP metrics.
**[V]** Final checks: 66 tests passed, one opt-in live test skipped; lint,
formatting and dependency checks passed. The real role sessions ran separately.
No commits or pushes were performed.
