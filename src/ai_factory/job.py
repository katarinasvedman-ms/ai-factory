from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .contracts import FactoryError
from .execution import prepare, verify
from .provider import ToolboxProviderResolver
from .resolvers import FoundryResolver, GitHubResolver, LocalResolver


def main() -> None:
    """Worker initialization is deterministic application code, never a model prompt."""
    artifacts = Path(os.environ["FACTORY_ARTIFACT_ROOT"])
    resume = os.environ.get("FACTORY_RESUME_EXECUTION")
    if resume:
        import uuid

        if str(uuid.UUID(resume)) != resume:
            raise FactoryError("Resume requires an exact execution UUID")
        directory = artifacts / resume
        verify(directory)
    else:
        source = json.loads(os.environ["FACTORY_SOURCE"])
        roles = json.loads(os.environ.get("FACTORY_ROLES", '["coder", "tester"]'))
        if source["kind"] == "github":
            resolver = GitHubResolver(source["repository"], source["commit"])
        elif source["kind"] == "foundry":
            mode = source.get("skill_mode", "bridge")
            if mode == "provider":
                resolver = ToolboxProviderResolver(source["binding"], roles=roles)
            elif mode == "bridge":
                approved = LocalResolver(Path(source["approved_skills"])).resolve()
                resolver = FoundryResolver(approved, source["binding"])
            else:
                raise FactoryError("Unknown Foundry skill mode")
        elif source["kind"] == "local":
            resolver = LocalResolver(Path(source["path"]))
        else:
            raise FactoryError("Unknown distribution source")
        directory = prepare(
            Path(os.environ["FACTORY_TARGET"]),
            resolver.resolve(),
            artifacts,
            json.loads(os.environ["FACTORY_REQUEST"]),
            json.loads(os.environ["FACTORY_POLICY"]),
            roles,
            json.loads(os.environ.get("FACTORY_TASKS", '["implementation", "testing"]')),
        )
    print(json.dumps({"execution": str(directory), "status": "prepared"}), flush=True)
    if os.environ.get("FACTORY_LIVE") == "1":
        from .runtime import run

        asyncio.run(run(directory, os.environ["FACTORY_MODEL"]))
        print(json.dumps({"execution": str(directory), "status": "approval-required"}), flush=True)


if __name__ == "__main__":
    main()
