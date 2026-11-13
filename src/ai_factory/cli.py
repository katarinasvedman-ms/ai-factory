from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .contracts import ROLES, TASKS, FactoryError
from .execution import prepare, verify
from .provider import ToolboxProviderResolver
from .resolvers import FoundryResolver, GitHubResolver, LocalResolver


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Reproducible, proposal-only AI factory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("runtime-info", help="Start the pinned SDK runtime without a model call")
    init = commands.add_parser(
        "prepare", help="Resolve, pin, and persist an execution; no model calls"
    )
    init.add_argument("--target", required=True, type=Path)
    init.add_argument("--source", choices=["local", "github", "foundry"], default="local")
    init.add_argument(
        "--skills", type=Path, help="Approved local skill tree (also needed for Foundry)"
    )
    init.add_argument("--github-repository", default="katarinasvedman-ms/factory-skills")
    init.add_argument("--github-commit", help="Approved full commit SHA, not a floating tag")
    init.add_argument("--foundry-binding", type=Path)
    init.add_argument("--skill-mode", choices=["bridge", "provider"], default="bridge")
    init.add_argument("--output", type=Path, default=Path(".artifacts/executions"))
    init.add_argument("--request", required=True, type=Path)
    init.add_argument("--policy", required=True, type=Path)
    init.add_argument("--roles", nargs="+", choices=sorted(ROLES), default=["coder", "tester"])
    init.add_argument(
        "--tasks", nargs="+", choices=sorted(TASKS), default=["implementation", "testing"]
    )
    check = commands.add_parser("verify", help="Verify persisted target and skill integrity")
    check.add_argument("execution", type=Path)
    live = commands.add_parser("run", help="Run SDK sessions using existing authentication")
    live.add_argument("execution", type=Path)
    live.add_argument("--model", required=True)
    live.add_argument("--roles", nargs="+", choices=sorted(ROLES))
    live.add_argument("--timeout", type=float, default=180)
    live.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="Explicit consent to authenticated model calls",
    )
    args = parser.parse_args()
    try:
        if args.command == "runtime-info":
            from .runtime import runtime_info

            print(json.dumps(asyncio.run(runtime_info())))
        elif args.command == "prepare":
            if args.skill_mode == "provider":
                if args.source != "foundry" or args.foundry_binding is None:
                    raise FactoryError("Provider mode needs --source foundry and --foundry-binding")
                resolver = ToolboxProviderResolver(
                    load_json(args.foundry_binding), roles=args.roles
                )
            elif args.source == "github":
                resolver = GitHubResolver(args.github_repository, args.github_commit or "")
            else:
                if args.skills is None:
                    raise FactoryError("--skills is required for local and Foundry sources")
                approved = LocalResolver(args.skills).resolve()
                if args.source == "foundry":
                    if args.foundry_binding is None:
                        raise FactoryError("--foundry-binding is required")
                    resolver = FoundryResolver(approved, load_json(args.foundry_binding))
                else:
                    resolver = LocalResolver(args.skills)
            directory = prepare(
                args.target,
                resolver.resolve(),
                args.output,
                load_json(args.request),
                load_json(args.policy),
                args.roles,
                args.tasks,
            )
            print(directory)
        elif args.command == "verify":
            manifest = verify(args.execution)
            print(json.dumps({"execution_id": manifest["execution_id"], "integrity": "verified"}))
        else:
            from .runtime import run

            results = asyncio.run(
                run(args.execution, args.model, roles=args.roles, timeout=args.timeout)
            )
            print(
                json.dumps(
                    {
                        role: {"status": result["status"], "skills": result["skills_activated"]}
                        for role, result in results.items()
                    },
                    indent=2,
                )
            )
    except (FactoryError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"factory: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
