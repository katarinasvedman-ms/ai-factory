"""Probe Toolbox authorization, temporarily narrowing only this PoC's coder grant."""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential
from setup_role_isolation import role_definition_id

from ai_factory.contracts import FactoryError
from ai_factory.execution import write_json
from ai_factory.provider import ToolboxClient


async def probe(binding, credential=None):
    client = ToolboxClient(binding, credential=credential)
    results = {}
    try:
        for operation, params, notification in (
            (
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "factory-role-rbac-probe", "version": "0.1.0"},
                },
                False,
            ),
            ("notifications/initialized", None, True),
            ("tools/list", {}, False),
            ("resources/list", {}, False),
            ("resources/read", {"uri": "skill://dotnet-development/SKILL.md"}, False),
        ):
            try:
                result = await client.rpc(operation, params, notification=notification)
                if operation == "initialize":
                    client.headers["MCP-Protocol-Version"] = result["protocolVersion"]
                results[operation] = {
                    "http_status": client.calls[-1]["status"],
                    "result": result,
                }
            except FactoryError as exc:
                results[operation] = {
                    "http_status": client.calls[-1].get("status"),
                    "error": str(exc),
                }
        return {
            "toolbox": binding["toolbox"],
            "identity": binding["identity"],
            "operations": results,
            "calls": client.metrics()["calls"],
        }
    finally:
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args()
    state = json.loads((args.root / "state.json").read_text())
    scope = state["project_scope"]
    role_definition = role_definition_id(scope)
    scoped = json.loads((args.root / "toolbox-scope-probe.json").read_text())
    assert scoped["assignment_http_status"] in (200, 201)
    project_assignment = state["identities"]["coder"]["project_assignment_id"]
    identity = state["toolboxes"]["coder"]["identity"]
    evidence = {"scope_assignment": scoped, "phases": [], "management_requests": []}

    def persist():
        write_json(args.root / "rbac-access.json", evidence, atomic=True)

    with DefaultAzureCredential(exclude_interactive_browser_credential=True) as operator:
        with httpx.Client(timeout=45) as management:

            def arm(method, path, body=None):
                response = management.request(
                    method,
                    f"https://management.azure.com{path}?api-version=2022-04-01",
                    json=body,
                    headers={
                        "Authorization": "Bearer "
                        + operator.get_token("https://management.azure.com/.default").token
                    },
                )
                evidence["management_requests"].append(
                    {"method": method, "path": path, "http_status": response.status_code}
                )
                persist()
                response.raise_for_status()
                return response

            def sample(label):
                observations = {}
                for target in ("coder", "tester"):
                    binding = {
                        **state["toolboxes"][target],
                        "identity": identity,
                    }
                    observations[target] = asyncio.run(probe(binding))
                evidence["phases"].append({"label": label, "access": observations})
                persist()
                print(
                    json.dumps(
                        {
                            "phase": label,
                            "coder_tools_list": observations["coder"]["operations"]["tools/list"][
                                "http_status"
                            ],
                            "tester_tools_list": observations["tester"]["operations"]["tools/list"][
                                "http_status"
                            ],
                        }
                    ),
                    flush=True,
                )
                return observations

            existing = arm("GET", project_assignment).json()["properties"]
            assert existing["principalId"] == identity["principal_id"]
            assert existing["scope"].lower() == scope.lower()
            assert existing["roleDefinitionId"].lower() == role_definition.lower()
            os.environ.update(
                AZURE_TOKEN_CREDENTIALS="EnvironmentCredential",
                AZURE_CLIENT_ID=identity["client_id"],
                AZURE_TENANT_ID=identity["tenant_id"],
                AZURE_CLIENT_CERTIFICATE_PATH=str((args.credentials / "coder.pem").resolve()),
            )
            for variable in ("AZURE_CLIENT_SECRET", "AZURE_USERNAME", "AZURE_PASSWORD"):
                os.environ.pop(variable, None)
            sample("project-and-toolbox-grants")
            try:
                arm("DELETE", project_assignment)
                for attempt in range(4):
                    if attempt:
                        time.sleep(20)
                    access = sample(f"toolbox-grant-only-{attempt + 1}")
                    if access["tester"]["operations"]["tools/list"]["http_status"] == 403:
                        break
            finally:
                arm(
                    "PUT",
                    project_assignment,
                    {
                        "properties": {
                            "roleDefinitionId": role_definition,
                            "principalId": identity["principal_id"],
                            "principalType": "ServicePrincipal",
                        }
                    },
                )
                evidence["project_grant_restored"] = True
                arm("DELETE", scoped["assignment_response"]["id"])
                evidence["temporary_toolbox_grant_removed"] = True
                persist()
            sample("restored-project-grant")


if __name__ == "__main__":
    main()
