from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .contracts import MAX_BYTES, MAX_FILES, FactoryError, canonical, digest

IGNORED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "bin",
    "obj",
    ".artifacts",
    ".agents",
    ".github",
    "__pycache__",
    "dist",
    ".pytest_cache",
    ".ruff_cache",
}


def repository_files(root: Path) -> dict[str, bytes]:
    root = root.resolve()
    if not root.is_dir():
        raise FactoryError("Target repository does not exist")
    result, total = {}, 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(
            d for d in directories if d not in IGNORED and not (Path(current) / d).is_symlink()
        )
        for name in sorted(filenames):
            path = Path(current) / name
            if path.is_symlink():
                continue
            size = path.stat().st_size
            if size > 1024 * 1024:
                raise FactoryError(f"PoC target file exceeds 1 MiB: {path.name}")
            total += size
            if total > MAX_BYTES or len(result) >= MAX_FILES:
                raise FactoryError("PoC target repository exceeds snapshot limits")
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def profile(files: dict[str, bytes]) -> dict:
    evidence: dict[str, list[str]] = {}

    def add(technology, path, reason):
        evidence.setdefault(technology, []).append(f"{path}: {reason}")

    for name, content in sorted(files.items()):
        filename = Path(name).name
        text = content.decode("utf-8", errors="replace")
        if filename.endswith(".csproj"):
            add("dotnet", name, "C# project manifest")
            try:
                project = ET.fromstring(text)
            except ET.ParseError as exc:
                raise FactoryError(f"Invalid project XML: {name}") from exc
            references = [
                node.attrib.get("Include", "")
                for node in project.iter()
                if node.tag.rsplit("}", 1)[-1] == "PackageReference"
            ]
            if "Microsoft.NET.Sdk.Web" in text or any("AspNetCore" in r for r in references):
                add("api", name, "ASP.NET Core web SDK or package")
            if any(
                r in {"Microsoft.EntityFrameworkCore.SqlServer", "Microsoft.Data.SqlClient"}
                for r in references
            ):
                add("sqlserver", name, "SQL Server-specific package reference")
        if filename in {"pom.xml", "build.gradle", "build.gradle.kts"}:
            add("java", name, "Maven/Gradle build manifest")
        if filename in {"pyproject.toml", "requirements.txt", "setup.py", "Pipfile"}:
            add("python", name, "Python dependency/build manifest")
        if re.search(r"(?i)(Server|Data Source)\s*=.+;.*(Database|Initial Catalog)\s*=", text):
            if filename.startswith("appsettings") and filename.endswith(".json"):
                try:
                    json.loads(text)
                except json.JSONDecodeError as exc:
                    raise FactoryError(f"Invalid application settings JSON: {name}") from exc
                add("sqlserver", name, "SQL Server-style connection string")
        if filename.lower() in {"openapi.json", "openapi.yaml", "openapi.yml"}:
            add("api", name, "OpenAPI document")
    return {
        "technologies": sorted(evidence),
        "evidence": evidence,
        "status": "detected" if evidence else "unknown",
        "snapshot_sha256": digest(canonical({k: digest(v) for k, v in sorted(files.items())})),
    }
