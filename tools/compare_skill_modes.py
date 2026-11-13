"""Opt-in, read-only-on-Azure bridge/provider comparison using real SDK sessions."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ai_factory.contracts import FactoryError
from ai_factory.execution import prepare, verify, write_json
from ai_factory.profile import profile, repository_files
from ai_factory.provider import ToolboxProviderResolver
from ai_factory.resolvers import FoundryResolver, LocalResolver, Transport
from ai_factory.runtime import run


class MeasuredBridgeTransport(Transport):
    def __init__(self):
        super().__init__(httpx.Client(timeout=30, follow_redirects=False))
        self.calls = []

    def request(self, method, url, **kwargs):
        started = time.perf_counter()
        response = super().request(method, url, **kwargs)
        body = kwargs.get("json", {})
        self.calls.append(
            {
                "operation": body.get(
                    "method", "package" if "/skills/" in url else "toolbox.version"
                ),
                "uri": body.get("params", {}).get("uri"),
                "path": response.request.url.path,
                "bytes": len(response.content),
                "status": response.status_code,
                "seconds": time.perf_counter() - started,
            }
        )
        return response


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--skills", required=True, type=Path, help="Used only by bridge mode")
    parser.add_argument("--target", type=Path, default=Path("tests/fixtures/orders"))
    parser.add_argument("--request", type=Path, default=Path("examples/request.json"))
    parser.add_argument("--policy", type=Path, default=Path("examples/policy.json"))
    parser.add_argument(
        "--provider-policy", type=Path, default=Path("examples/provider-policy.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path(".artifacts/toolbox-provider-spike/comparison")
    )
    parser.add_argument(
        "--modes", nargs="+", choices=["bridge", "provider"], default=["bridge", "provider"]
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    before = profile(repository_files(args.target))["snapshot_sha256"]
    args.output.mkdir(parents=True, exist_ok=True)
    comparison = {}
    for mode in args.modes:
        print(f"Running {mode} comparison...", flush=True)
        started = datetime.now(UTC)
        timer = time.perf_counter()
        if mode == "provider":
            resolver = ToolboxProviderResolver(load(args.binding))
            snapshot = resolver.resolve()
            policy = load(args.provider_policy)
            discovery_calls = resolver.metrics["calls"]
        else:
            transport = MeasuredBridgeTransport()
            resolver = FoundryResolver(
                LocalResolver(args.skills).resolve(), load(args.binding), transport
            )
            try:
                snapshot = resolver.resolve()
            finally:
                transport.client.close()
                if resolver.credential:
                    resolver.credential.close()
            policy = load(args.policy)
            discovery_calls = transport.calls
        directory = prepare(
            args.target,
            snapshot,
            args.output / mode,
            load(args.request),
            policy,
            ["coder", "tester"],
            ["implementation", "testing"],
        )
        preparation_seconds = time.perf_counter() - timer
        results = asyncio.run(run(directory, args.model, timeout=240))
        manifest = verify(directory)
        events = [
            json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()
        ]
        ready = [item for item in events if item["kind"] == "session.ready"]
        calls = list(discovery_calls)
        for role in manifest["roles"]:
            metrics_path = directory / f"{role}.provider-metrics.json"
            if metrics_path.exists():
                metrics = load(metrics_path)
                assert metrics["temporary_directory_removed"]
                calls.extend(metrics["calls"])
        instruction_reads = [
            row["uri"]
            for row in calls
            if row["operation"] == "resources/read" and (row.get("uri") or "").endswith("/SKILL.md")
        ]
        native_path = directory / "native-skill-events.jsonl"
        native = [json.loads(line) for line in native_path.read_text().splitlines()]
        assert native and all(
            item["type"] in {"skill.invoked", "skill.invoked.ref"} for item in native
        )
        assert "dotnet-development" in results["coder"]["skills_activated"]
        assert "test-strategy" in results["tester"]["skills_activated"]
        assert list((directory / "proposals").glob("*.json"))
        assert any(item["kind"] == "skill.resource_read" for item in events)
        assert before == profile(repository_files(args.target))["snapshot_sha256"]
        forbidden = {"java-development", "python-development", "database-changes"}
        assert not forbidden & {item["data"]["name"] for item in native}
        if mode == "provider":
            assert not any(
                any(f"skill://{name}/" in uri for name in forbidden) for uri in instruction_reads
            )
            assert not (directory / "skills").exists()
        comparison[mode] = {
            "execution": str(directory.resolve()),
            "model": args.model,
            "started_at": started.isoformat(),
            "startup_to_first_session_ready_seconds": (
                datetime.fromisoformat(ready[0]["time"]) - started
            ).total_seconds(),
            "preparation_seconds": preparation_seconds,
            "sdk_session_ready_seconds": {row["role"]: row["startup_seconds"] for row in ready},
            "toolbox_response_body_bytes": sum(row["bytes"] for row in calls),
            "skill_instruction_fetches": len(instruction_reads),
            "unique_skills_fetched": sorted(set(instruction_reads)),
            "native_activation_event_count": len(native),
            "activated_by_role": {
                role: result["skills_activated"] for role, result in results.items()
            },
            "supporting_resource_reads": [
                item for item in events if item["kind"] == "skill.resource_read"
            ],
            "calls": calls,
            "target_snapshot_unchanged": True,
            "total_seconds": time.perf_counter() - timer,
        }
        write_json(args.output / "comparison.json", comparison, atomic=True)
        print(
            json.dumps(
                {key: value for key, value in comparison[mode].items() if key != "calls"},
                indent=2,
            ),
            flush=True,
        )
    if len(comparison) == 2:
        a = load(Path(comparison["bridge"]["execution"]) / "manifest.json")
        b = load(Path(comparison["provider"]["execution"]) / "manifest.json")
        if a["assignments"] != b["assignments"] or a["request"] != b["request"]:
            raise FactoryError("Comparison scenarios or role permissions differ")


if __name__ == "__main__":
    main()
