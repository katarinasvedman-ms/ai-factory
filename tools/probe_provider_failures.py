"""Verify fail-closed behavior with the real SDK and controlled transport faults."""

import argparse
import asyncio
import json
from functools import partial
from pathlib import Path

from copilot import CopilotClient

from ai_factory.contracts import FactoryError
from ai_factory.execution import prepare, verify, write_json
from ai_factory.provider import ProviderSnapshot, ToolboxClient, ToolboxSkillProvider
from ai_factory.runtime import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    baseline = verify(args.execution)
    discovery = json.loads((args.execution / "discovery.json").read_text())
    policy = dict(baseline["policy"])
    policy["permitted_skills"] = {"coder": baseline["assignments"]["coder"]}
    observations = {}
    args.output.mkdir(parents=True, exist_ok=True)
    for phase in ("startup", "mid-session"):
        native_events = []

        class FaultClient(ToolboxClient):
            async def request(self, method, url, *, body=None):
                activation = (
                    body
                    and body.get("method") == "resources/read"
                    and body.get("params", {}).get("uri") != "skill://index.json"
                )
                if phase == "startup" or activation:
                    raise FactoryError("Toolbox transport unavailable (controlled injection)")
                return await super().request(method, url, body=body)

        class ObservedSDK(CopilotClient):
            async def create_session(self, **configuration):
                original = configuration["on_event"]

                def observed(notification):
                    kind = getattr(notification.type, "value", str(notification.type))
                    if kind in {
                        "skill.invoked",
                        "skill.invoked.ref",
                        "tool.execution_start",
                        "tool.execution_complete",
                    }:
                        native_events.append(notification.to_dict())
                    original(notification)

                configuration["on_event"] = observed
                return await super().create_session(**configuration)

        directory = prepare(
            args.execution / "target",
            ProviderSnapshot(discovery, baseline["distribution"]),
            args.output / phase,
            baseline["request"],
            policy,
            ["coder"],
            baseline["tasks"],
        )
        try:
            asyncio.run(
                run(
                    directory,
                    args.model,
                    timeout=120,
                    client_factory=ObservedSDK,
                    provider_factory=partial(ToolboxSkillProvider, client_factory=FaultClient),
                )
            )
        except FactoryError as exc:
            error = str(exc)
        else:
            raise AssertionError("SDK run silently succeeded after injected Toolbox failure")
        assert "controlled injection" in error
        metrics = json.loads((directory / "coder.provider-metrics.json").read_text())
        assert metrics["temporary_directory_removed"]
        assert not (directory / "coder.result.json").exists()
        assert not (directory / "proposals").exists()
        assert not any(event["type"].startswith("skill.invoked") for event in native_events)
        write_json(directory / "native-failure-events.json", native_events)
        observations[phase] = {
            "execution": str(directory.resolve()),
            "error": error,
            "real_sdk": True,
            "controlled_transport_fault": True,
            "native_events": native_events,
            "temporary_directory_removed": True,
            "success_result_created": False,
            "proposal_created": False,
        }
        write_json(args.output / "failures.json", observations, atomic=True)
        print(
            json.dumps(
                {phase: {k: v for k, v in observations[phase].items() if k != "native_events"}}
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
