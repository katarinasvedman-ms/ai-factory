from __future__ import annotations

import importlib.metadata
import json
import stat
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .contracts import CLI_VERSION, FactoryError, Snapshot, canonical, digest, relative_path
from .profile import profile, repository_files
from .provider import ProviderSnapshot
from .selection import select


def write_json(path: Path, value: object, *, atomic=False) -> None:
    destination = path.with_name(path.name + ".tmp") if atomic else path
    destination.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    if atomic:
        destination.replace(path)


def write_files(root: Path, files: dict[str, bytes], *, readonly=False) -> None:
    for name, content in files.items():
        path = root / relative_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if readonly:
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def git_identity(root: Path) -> dict:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode:
        return {
            "commit": None,
            "dirty": None,
            "note": "Local fixture; identified by snapshot digest",
        }
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return {"commit": result.stdout.strip(), "dirty": bool(status.stdout.strip())}


def prepare(
    target: Path,
    snapshot: Snapshot | ProviderSnapshot,
    output: Path,
    request: dict,
    policy: dict,
    roles: list[str],
    tasks: list[str],
) -> Path:
    if not isinstance(request.get("description"), str) or not request["description"].strip():
        raise FactoryError("Request needs a nonempty description")
    if not request.get("id"):
        raise FactoryError("Request needs an identifier")
    target = target.resolve()
    if output.resolve().is_relative_to(target):
        raise FactoryError("Execution output must be outside the target repository")
    files = repository_files(target)
    if not files:
        raise FactoryError("Target snapshot is empty")
    characteristics = profile(files)
    provider = isinstance(snapshot, ProviderSnapshot)
    assignments = (
        snapshot.assignments(policy, roles)
        if provider
        else select(
            snapshot,
            characteristics["technologies"],
            roles,
            tasks,
            policy["allowed_skills"],
            policy.get("required_skills"),
        )
    )
    execution_id = str(uuid.uuid4())
    directory = output.resolve() / execution_id
    directory.mkdir(parents=True, exist_ok=False)
    write_files(directory / "target", files, readonly=True)
    selected = sorted({name for names in assignments.values() for name in names})
    if provider:
        write_json(directory / "discovery.json", snapshot.records)
    else:
        for role, names in assignments.items():
            for name in names:
                write_files(
                    directory / "skills" / role / name, snapshot.skill_files(name), readonly=True
                )
    manifest = {
        "schema_version": 1,
        "execution_id": execution_id,
        "created_at": datetime.now(UTC).isoformat(),
        "request": request,
        "request_sha256": digest(canonical(request)),
        "target": {
            "repository": request.get("repository", str(target)),
            **git_identity(target),
            "snapshot_sha256": characteristics["snapshot_sha256"],
            "files": {name: digest(content) for name, content in sorted(files.items())},
        },
        "profile": characteristics,
        "roles": roles,
        "tasks": tasks,
        "assignments": assignments,
        "distribution": snapshot.source,
        "catalog_sha256": snapshot.catalog_sha256,
        "skills": {name: snapshot.records[name] for name in selected},
        "policy": policy,
        "policy_sha256": digest(canonical(policy)),
        "runtime": {
            "package": "github-copilot-sdk",
            "version": importlib.metadata.version("github-copilot-sdk"),
            "cli_version": CLI_VERSION,
            "tools": "read-only snapshot and proposal artifacts; no shell, commit, push or deploy",
        },
    }
    if provider:
        manifest["activations"] = []
    write_json(directory / "manifest.json", manifest)
    (directory / "events.jsonl").touch()
    verify(directory)
    return directory


def verify(directory: Path) -> dict:
    if directory.is_symlink() or (directory / "manifest.json").is_symlink():
        raise FactoryError("Execution paths cannot be symlinks")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != 1:
        raise FactoryError("Unsupported execution schema")
    if digest(canonical(manifest["request"])) != manifest["request_sha256"]:
        raise FactoryError("Execution request has changed")
    if digest(canonical(manifest["policy"])) != manifest["policy_sha256"]:
        raise FactoryError("Execution policy has changed")
    actual_target = repository_files(directory / "target")
    hashes = {name: digest(content) for name, content in sorted(actual_target.items())}
    if hashes != manifest["target"]["files"]:
        raise FactoryError("Execution target snapshot has changed")
    if digest(canonical(hashes)) != manifest["target"]["snapshot_sha256"]:
        raise FactoryError("Execution target digest mismatch")
    if set(manifest["assignments"]) != set(manifest["roles"]):
        raise FactoryError("Role assignments do not match manifest")
    if manifest["distribution"].get("skill_mode") == "provider":
        discovery = json.loads((directory / "discovery.json").read_text(encoding="utf-8"))
        if digest(canonical(discovery)) != manifest["catalog_sha256"]:
            raise FactoryError("Pinned discovery metadata changed")
        snapshot = ProviderSnapshot(discovery, manifest["distribution"])
        if snapshot.assignments(manifest["policy"], manifest["roles"]) != manifest["assignments"]:
            raise FactoryError("Provider role assignments changed")
        selected = {name for names in manifest["assignments"].values() for name in names}
        if set(manifest["skills"]) != selected:
            raise FactoryError("Unexpected provider skill records")
        for name in selected:
            record = manifest["skills"][name]
            if any(record.get(key) != value for key, value in discovery[name].items()) or record[
                "foundry_version"
            ] != manifest["distribution"]["skill_versions"].get(name):
                raise FactoryError("Provider skill pin or descriptor changed")
            if "files" in record:
                for path in record["files"]:
                    relative_path(path)
                if digest(canonical(record["files"])) != record.get("sha256"):
                    raise FactoryError("Activated provider skill digest changed")
        for activation in manifest["activations"]:
            if (
                activation["role"] not in manifest["assignments"]
                or activation["skill"] not in manifest["assignments"][activation["role"]]
            ):
                raise FactoryError("Unapproved activation in provider manifest")
        return manifest
    for role, names in manifest["assignments"].items():
        role_root = directory / "skills" / role
        if role_root.is_symlink() or {p.name for p in role_root.iterdir()} != set(names):
            raise FactoryError(f"Unexpected skills in role directory: {role}")
        for name in names:
            root = role_root / name
            # Reuse the package walker without depending on the current upstream catalog.
            files = local_files_for_skill(root)
            record = manifest["skills"][name]
            actual = {key: digest(value) for key, value in sorted(files.items())}
            if actual != record["files"] or digest(canonical(actual)) != record["sha256"]:
                raise FactoryError(f"Pinned skill integrity mismatch: {role}/{name}")
    return manifest


def local_files_for_skill(root: Path) -> dict[str, bytes]:
    if root.is_symlink():
        raise FactoryError("Skill directory cannot be a symlink")
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise FactoryError("Skill resource cannot be a symlink")
        if path.is_file():
            files[relative_path(path.relative_to(root).as_posix())] = path.read_bytes()
    return files


def event(directory: Path, kind: str, **details) -> None:
    record = {"time": datetime.now(UTC).isoformat(), "kind": kind, **details}
    with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
