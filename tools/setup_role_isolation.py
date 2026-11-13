"""Publish role Toolbox pins and provision certificate-only local test identities."""

import argparse
import base64
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from ai_factory.contracts import ROLES, FactoryError
from ai_factory.execution import write_json

FOUNDRY_USER_ROLE_ID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"


def role_definition_id(project_scope):
    subscription_scope = project_scope.split("/resourceGroups/", 1)[0]
    return (
        f"{subscription_scope}/providers/Microsoft.Authorization/roleDefinitions/"
        f"{FOUNDRY_USER_ROLE_ID}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--project-scope", required=True, help="Foundry project's full ARM ID")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args()
    project = args.project_endpoint.rstrip("/")
    scope = args.project_scope.rstrip("/")
    if not project.startswith("https://") or not scope.startswith("/subscriptions/"):
        raise FactoryError("Explicit HTTPS project endpoint and ARM project scope are required")
    tenant = str(uuid.UUID(args.tenant_id))
    role_definition = role_definition_id(scope)
    args.output.mkdir(parents=True, exist_ok=True)
    state_path = args.output / "state.json"
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {"project_scope": scope, "toolboxes": {}, "identities": {}, "requests": []}
    )
    if state["project_scope"] != scope:
        raise FactoryError("Existing state belongs to another project scope")
    if any(item["tenant_id"] != tenant for item in state["identities"].values()) or any(
        item["project_endpoint"] != project for item in state["toolboxes"].values()
    ):
        raise FactoryError("Existing state belongs to another tenant or project endpoint")

    def persist():
        write_json(state_path, state, atomic=True)

    with DefaultAzureCredential(exclude_interactive_browser_credential=True) as credential:
        with httpx.Client(timeout=45, follow_redirects=False) as http:

            def request(method, url, *, body=None, audience="https://ai.azure.com/.default"):
                token = credential.get_token(audience).token
                headers = {"Authorization": f"Bearer {token}"}
                if audience == "https://ai.azure.com/.default":
                    headers["Foundry-Features"] = "Skills=V1Preview"
                response = http.request(method, url, json=body, headers=headers)
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                state["requests"].append(
                    {
                        "method": method,
                        "url": url,
                        "status": response.status_code,
                        "error": data.get("error"),
                    }
                )
                persist()
                if not response.is_success:
                    code = data.get("error", {}).get("code", "unknown")
                    raise FactoryError(f"HTTP {response.status_code}: {code}")
                return data

            original = request(
                "GET", f"{project}/toolboxes/factory-skills/versions/1?api-version=v1"
            )
            pins = {item["name"]: item["version"] for item in original["skills"]}
            catalog = json.loads(args.catalog.read_text())
            for role in sorted(ROLES):
                name = f"{role}-skills"
                selected = {
                    item["id"]: pins[item["id"]]
                    for item in catalog["skills"]
                    if role in item["roles"]
                }
                if role not in state["toolboxes"]:
                    result = request(
                        "POST",
                        f"{project}/toolboxes/{name}/versions?api-version=v1",
                        body={
                            "description": f"Factory {role}; immutable factory-skills v1 pins.",
                            "tools": [],
                            "skills": [
                                {"type": "skill_reference", "name": skill, "version": version}
                                for skill, version in sorted(selected.items())
                            ],
                        },
                    )
                    state["toolboxes"][role] = {
                        "project_endpoint": project,
                        "toolbox": name,
                        "version": result["version"],
                        "skills": selected,
                    }
                    persist()
                binding = state["toolboxes"][role]
                actual = request(
                    "GET",
                    f"{project}/toolboxes/{name}/versions/{binding['version']}?api-version=v1",
                )
                if {item["name"]: item["version"] for item in actual["skills"]} != selected:
                    raise FactoryError(f"Role toolbox pins differ: {role}")
                print(
                    f"{role}: {name} v{binding['version']}, {len(selected)} pinned skills",
                    flush=True,
                )

            args.credentials.mkdir(parents=True, exist_ok=True)
            sid = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "icacls",
                    str(args.credentials),
                    "/inheritance:r",
                    "/grant:r",
                    f"*{sid}:(OI)(CI)F",
                    "*S-1-5-18:(OI)(CI)F",
                ],
                check=True,
                capture_output=True,
            )
            for role in sorted(ROLES):
                if role not in state["identities"]:
                    name = f"ai-factory-{role}-local"
                    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                    now = datetime.now(UTC)
                    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
                    certificate = (
                        x509.CertificateBuilder()
                        .subject_name(subject)
                        .issuer_name(subject)
                        .public_key(key.public_key())
                        .serial_number(x509.random_serial_number())
                        .not_valid_before(now - timedelta(minutes=5))
                        .not_valid_after(now + timedelta(days=1))
                        .sign(key, hashes.SHA256())
                    )
                    application = request(
                        "POST",
                        "https://graph.microsoft.com/v1.0/applications",
                        audience="https://graph.microsoft.com/.default",
                        body={
                            "displayName": name,
                            "signInAudience": "AzureADMyOrg",
                            "keyCredentials": [
                                {
                                    "type": "AsymmetricX509Cert",
                                    "usage": "Verify",
                                    "displayName": "One-day factory role isolation test",
                                    "key": base64.b64encode(
                                        certificate.public_bytes(serialization.Encoding.DER)
                                    ).decode(),
                                }
                            ],
                        },
                    )
                    pem = args.credentials / f"{role}.pem"
                    pem.write_bytes(
                        key.private_bytes(
                            serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption(),
                        )
                        + certificate.public_bytes(serialization.Encoding.PEM)
                    )
                    state["identities"][role] = {
                        "type": "service_principal",
                        "application_object_id": application["id"],
                        "client_id": application["appId"],
                        "tenant_id": tenant,
                        "certificate_expires": (now + timedelta(days=1)).isoformat(),
                    }
                    persist()
                identity = state["identities"][role]
                if "principal_id" not in identity:
                    principal = request(
                        "POST",
                        "https://graph.microsoft.com/v1.0/servicePrincipals",
                        audience="https://graph.microsoft.com/.default",
                        body={"appId": identity["client_id"], "accountEnabled": True},
                    )
                    identity["principal_id"] = principal["id"]
                    persist()
                assignment = str(uuid.uuid5(uuid.NAMESPACE_URL, scope + identity["principal_id"]))
                assignment_url = (
                    f"https://management.azure.com{scope}"
                    f"/providers/Microsoft.Authorization/roleAssignments/{assignment}"
                    "?api-version=2022-04-01"
                )
                result = request(
                    "PUT",
                    assignment_url,
                    audience="https://management.azure.com/.default",
                    body={
                        "properties": {
                            "roleDefinitionId": role_definition,
                            "principalId": identity["principal_id"],
                            "principalType": "ServicePrincipal",
                        }
                    },
                )
                identity["project_assignment_id"] = result["id"]
                state["toolboxes"][role]["identity"] = {
                    key: identity[key] for key in ("type", "client_id", "tenant_id", "principal_id")
                }
                persist()
                print(
                    f"{role}: distinct local service principal, project Foundry User assigned",
                    flush=True,
                )
            write_json(
                args.output / "role-bindings.json", {"roles": state["toolboxes"]}, atomic=True
            )


if __name__ == "__main__":
    main()
