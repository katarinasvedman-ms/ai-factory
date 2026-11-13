"""Use a disposable, never-project-granted principal to remove RBAC cache ambiguity."""

import argparse
import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential
from azure.identity.aio import CertificateCredential
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from probe_role_rbac import probe
from setup_role_isolation import role_definition_id

from ai_factory.execution import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args()
    state = json.loads((args.root / "state.json").read_text())
    project_scope = state["project_scope"]
    role_definition = role_definition_id(project_scope)
    tenant = state["identities"]["coder"]["tenant_id"]
    evidence = {"management": [], "phases": {}, "never_project_granted": True}
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "factory-scope-control")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ) + certificate.public_bytes(serialization.Encoding.PEM)
    app = principal = assignment = None

    def persist():
        write_json(args.root / "fresh-scope-control.json", evidence, atomic=True)

    with DefaultAzureCredential(exclude_interactive_browser_credential=True) as operator:
        with httpx.Client(timeout=45) as client:

            def request(method, url, *, body=None, graph=False):
                scope = (
                    "https://graph.microsoft.com/.default"
                    if graph
                    else "https://management.azure.com/.default"
                )
                response = client.request(
                    method,
                    url,
                    json=body,
                    headers={"Authorization": "Bearer " + operator.get_token(scope).token},
                )
                evidence["management"].append(
                    {"method": method, "url": url, "http_status": response.status_code}
                )
                persist()
                response.raise_for_status()
                return response.json() if response.content else {}

            async def sample(target):
                credential = CertificateCredential(tenant, app["appId"], certificate_data=pem)
                return await probe(
                    {
                        **state["toolboxes"][target],
                        "identity": {
                            "client_id": app["appId"],
                            "principal_id": principal["id"],
                            "tenant_id": tenant,
                        },
                    },
                    credential,
                )

            def phase(label):
                evidence["phases"][label] = {
                    target: asyncio.run(sample(target)) for target in ("coder", "tester")
                }
                persist()
                print(
                    json.dumps(
                        {
                            "phase": label,
                            "access": {
                                target: {
                                    method: value["http_status"]
                                    for method, value in result["operations"].items()
                                    if method in {"tools/list", "resources/read"}
                                }
                                for target, result in evidence["phases"][label].items()
                            },
                        }
                    ),
                    flush=True,
                )

            try:
                app = request(
                    "POST",
                    "https://graph.microsoft.com/v1.0/applications",
                    graph=True,
                    body={
                        "displayName": "ai-factory-disposable-scope-control",
                        "signInAudience": "AzureADMyOrg",
                        "keyCredentials": [
                            {
                                "type": "AsymmetricX509Cert",
                                "usage": "Verify",
                                "key": base64.b64encode(
                                    certificate.public_bytes(serialization.Encoding.DER)
                                ).decode(),
                            }
                        ],
                    },
                )
                principal = request(
                    "POST",
                    "https://graph.microsoft.com/v1.0/servicePrincipals",
                    graph=True,
                    body={"appId": app["appId"]},
                )
                evidence["identity"] = {"client_id": app["appId"], "principal_id": principal["id"]}
                phase("no-grants")
                scope = project_scope + "/toolboxes/coder-skills"
                identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, scope + principal["id"]))
                assignment = (
                    f"https://management.azure.com{scope}"
                    f"/providers/Microsoft.Authorization/roleAssignments/{identifier}"
                    "?api-version=2022-04-01"
                )
                request(
                    "PUT",
                    assignment,
                    body={
                        "properties": {
                            "roleDefinitionId": role_definition,
                            "principalId": principal["id"],
                            "principalType": "ServicePrincipal",
                        }
                    },
                )
                phase("coder-toolbox-grant-only")
            finally:
                if assignment:
                    request("DELETE", assignment)
                if principal:
                    request(
                        "DELETE",
                        f"https://graph.microsoft.com/v1.0/servicePrincipals/{principal['id']}",
                        graph=True,
                    )
                if app:
                    request(
                        "DELETE",
                        f"https://graph.microsoft.com/v1.0/applications/{app['id']}",
                        graph=True,
                    )
                evidence["disposable_identity_removed"] = True
                persist()


if __name__ == "__main__":
    main()
