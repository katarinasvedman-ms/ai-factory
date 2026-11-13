from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

ROLES = {"architect", "planner", "coder", "reviewer", "tester"}
TASKS = {
    "architecture",
    "planning",
    "implementation",
    "review",
    "testing",
    "legacy",
    "api",
    "database",
}
TECHNOLOGIES = {"dotnet", "java", "python", "api", "sqlserver"}
RUNTIME = "copilot-sdk-python-1"
SDK_VERSION = "1.0.31"
CLI_VERSION = "1.0.109"
MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 2048
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class FactoryError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or re.search(r'[\x00-\x1f<>:"|?*\\]', value)
        or path.is_absolute()
        or path.as_posix() != value
        or any(p in {"", ".", ".."} or p.endswith((".", " ")) for p in path.parts)
        or any(
            re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", p) for p in path.parts
        )
    ):
        raise FactoryError(f"Unsafe relative path: {value!r}")
    return value


def unpack_zip(data: bytes, *, strip_root: bool = False) -> dict[str, bytes]:
    if len(data) > MAX_BYTES:
        raise FactoryError("Archive exceeds compressed size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES or sum(m.file_size for m in members) > MAX_BYTES:
                raise FactoryError("Archive exceeds extraction limits")
            files, seen = {}, set()
            roots = {m.filename.split("/")[0] for m in members}
            if strip_root and len(roots) != 1:
                raise FactoryError("Expected a single archive root")
            for member in members:
                relative_path(member.orig_filename.rstrip("/"))
                relative_path(member.filename.rstrip("/"))
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise FactoryError("Archive contains a link or special file")
                if member.is_dir():
                    continue
                name = member.filename.split("/", 1)[1] if strip_root else member.filename
                relative_path(name)
                if name.casefold() in seen:
                    raise FactoryError("Archive contains duplicate/colliding paths")
                seen.add(name.casefold())
                files[name] = archive.read(member)
            return files
    except (zipfile.BadZipFile, IndexError) as exc:
        raise FactoryError("Invalid skill archive") from exc


def local_files(root: Path) -> dict[str, bytes]:
    files = {}
    candidates = [root / "catalog.json", root / "release.json"]
    skills = root / "skills"
    if skills.is_symlink():
        raise FactoryError("Skill root cannot be a symlink")
    candidates.extend(skills.rglob("*"))
    total = 0
    for path in candidates:
        if path.is_symlink():
            raise FactoryError("Skill packages cannot contain symlinks")
        if path.is_file():
            total += path.stat().st_size
            if total > MAX_BYTES or len(files) >= MAX_FILES:
                raise FactoryError("Local skill snapshot exceeds limits")
            name = relative_path(path.relative_to(root).as_posix())
            files[name] = path.read_bytes()
    return files


@dataclass
class Snapshot:
    files: dict[str, bytes]
    source: dict

    def __post_init__(self):
        if "catalog.json" not in self.files:
            raise FactoryError("Missing catalog.json")
        self.catalog = json.loads(self.files["catalog.json"])
        if self.catalog.get("schema_version") != 1:
            raise FactoryError("Unsupported catalog schema")
        self.entries = {}
        self.records = {}
        for item in self.catalog["skills"]:
            name = item["id"]
            if not NAME.fullmatch(name) or len(name) > 64 or name in self.entries:
                raise FactoryError("Invalid or duplicate skill identifier")
            if item["path"] != f"skills/{name}":
                raise FactoryError(f"Invalid skill path: {name}")
            if not re.fullmatch(r"\d+\.\d+\.\d+", item["version"]):
                raise FactoryError(f"Invalid version: {name}")
            for field, permitted in (
                ("roles", ROLES),
                ("tasks", TASKS | {"*"}),
                ("technologies", TECHNOLOGIES | {"*"}),
            ):
                if not item[field] or not set(item[field]) <= permitted:
                    raise FactoryError(f"Invalid {field}: {name}")
            if not set(item["prerequisites"]) <= TECHNOLOGIES:
                raise FactoryError(f"Unknown prerequisites: {name}")
            prefix = item["path"] + "/"
            files = {
                relative_path(k[len(prefix) :]): v
                for k, v in self.files.items()
                if k.startswith(prefix)
            }
            if "SKILL.md" not in files:
                raise FactoryError(f"Missing instructions: {name}")
            sections = files["SKILL.md"].decode("utf-8").split("---", 2)
            if len(sections) != 3 or sections[0].strip():
                raise FactoryError(f"Missing frontmatter: {name}")
            front = yaml.safe_load(sections[1])
            if front.get("name") != name or not front.get("description") or not sections[2].strip():
                raise FactoryError(f"Invalid skill instructions: {name}")
            for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", sections[2]):
                if "://" not in link and not link.startswith("#"):
                    if relative_path(link) not in files:
                        raise FactoryError(f"Missing resource: {name}/{link}")
            hashes = {k: digest(v) for k, v in sorted(files.items())}
            self.entries[name] = item
            self.records[name] = {
                "version": item["version"],
                "sha256": digest(canonical(hashes)),
                "files": hashes,
            }
        self.catalog_sha256 = digest(self.files["catalog.json"])
        if "release.json" in self.files:
            release = json.loads(self.files["release.json"])
            if release != {
                "schema_version": 1,
                "catalog_sha256": self.catalog_sha256,
                "skills": self.records,
            }:
                raise FactoryError("Release digest mismatch")
        self._validate_dependencies()

    def _validate_dependencies(self):
        visited, active = set(), set()

        def visit(name):
            if name not in self.entries:
                raise FactoryError(f"Missing dependency: {name}")
            if name in active:
                raise FactoryError(f"Cyclic dependency: {name}")
            if name not in visited:
                active.add(name)
                for dependency in self.entries[name]["dependencies"]:
                    visit(dependency)
                active.remove(name)
                visited.add(name)

        for name in self.entries:
            visit(name)

    def skill_files(self, name: str) -> dict[str, bytes]:
        prefix = self.entries[name]["path"] + "/"
        return {
            key[len(prefix) :]: value for key, value in self.files.items() if key.startswith(prefix)
        }
