"""Run coder/tester in separate processes with distinct Entra certificate identities."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ai_factory.execution import prepare, verify, write_json
from ai_factory.profile import profile, repository_files
from ai_factory.provider import ToolboxProviderResolver


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    roles = ["coder", "tester"]
    bindings = load(args.binding)["roles"]
    target = Path("tests") / "fixtures" / "orders"
    before = profile(repository_files(target))["snapshot_sha256"]
    if args.resume:
        directory = args.resume.resolve()
        manifest = verify(directory)
        assert manifest["target"]["snapshot_sha256"] == before
        for role in roles:
            pinned = manifest["distribution"]["role_bindings"][role]
            assert pinned["identity"] == bindings[role]["identity"]
            assert pinned["toolbox"] == bindings[role]["toolbox"]
            assert pinned["skill_versions"] == bindings[role]["skills"]
    else:
        directory = prepare(
            target,
            ToolboxProviderResolver({"roles": bindings}, roles=roles).resolve(),
            args.output / "executions",
            load(Path("examples") / "request.json"),
            load(Path("examples") / "provider-policy.json"),
            roles,
            ["implementation", "testing"],
        )
    write_json(args.output / "execution.json", {"execution": str(directory)})
    evidence = {}
    for role in roles:
        identity = bindings[role]["identity"]
        environment = dict(os.environ)
        for key in ("AZURE_CLIENT_SECRET", "AZURE_USERNAME", "AZURE_PASSWORD"):
            environment.pop(key, None)
        environment.update(
            AZURE_TOKEN_CREDENTIALS="EnvironmentCredential",
            AZURE_TENANT_ID=identity["tenant_id"],
            AZURE_CLIENT_ID=identity["client_id"],
            AZURE_CLIENT_CERTIFICATE_PATH=str((args.credentials / f"{role}.pem").resolve()),
        )
        print(f"Running {role} with its own certificate identity and Toolbox...", flush=True)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "ai_factory.cli",
                "run",
                str(directory),
                "--roles",
                role,
                "--model",
                args.model,
                "--timeout",
                "180",
                "--live",
            ],
            env=environment,
            check=True,
        )
        metrics = load(directory / f"{role}.provider-metrics.json")
        assert metrics["temporary_directory_removed"]
        assert all(row["toolbox"] == bindings[role]["toolbox"] for row in metrics["calls"])
        assert all(
            row["identity"]["principal_id"] == identity["principal_id"] for row in metrics["calls"]
        )
        evidence[role] = {
            "identity": identity,
            "credential": (
                "DefaultAzureCredential -> EnvironmentCredential -> CertificateCredential"
            ),
            "toolbox": bindings[role]["toolbox"],
            "version": bindings[role]["version"],
            "result": load(directory / f"{role}.result.json"),
            "http_statuses": sorted({row["status"] for row in metrics["calls"]}),
            "temporary_directory_removed": True,
        }
    manifest = verify(directory)
    native = [
        json.loads(line)
        for line in (directory / "native-skill-events.jsonl").read_text().splitlines()
    ]
    assert native and all(item["data"]["source"] == "sdk" for item in native)
    assert "dotnet-development" in evidence["coder"]["result"]["skills_activated"]
    assert "test-strategy" in evidence["tester"]["result"]["skills_activated"]
    assert before == profile(repository_files(target))["snapshot_sha256"]
    write_json(
        args.output / "sessions.json",
        {
            "execution": str(directory),
            "roles": evidence,
            "native_events": native,
            "manifest_activations": manifest["activations"],
            "target_unchanged": True,
        },
        atomic=True,
    )
    print(
        json.dumps({"execution": str(directory), "distinct_identities_verified": True}), flush=True
    )


if __name__ == "__main__":
    main()
