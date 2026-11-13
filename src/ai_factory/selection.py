from __future__ import annotations

from .contracts import ROLES, RUNTIME, TASKS, FactoryError, Snapshot


def select(
    snapshot: Snapshot,
    technologies: list[str],
    roles: list[str],
    tasks: list[str],
    allowed: list[str],
    required: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    if not roles or not set(roles) <= ROLES or len(roles) != len(set(roles)):
        raise FactoryError("Provide unique known roles")
    if not tasks or not set(tasks) <= TASKS:
        raise FactoryError("Provide known task categories")
    approved, tech, required = set(allowed), set(technologies), required or {}
    if not approved <= snapshot.entries.keys():
        raise FactoryError("Policy allows a skill absent from the approved snapshot")
    if not required.keys() <= set(roles):
        raise FactoryError("Required skills reference a nonparticipating role")
    assignments = {}
    for role in roles:
        chosen = set()

        def include(name):
            if name not in approved:
                raise FactoryError(f"Skill is missing or unauthorized: {name}")
            entry = snapshot.entries[name]
            if role not in entry["roles"]:
                raise FactoryError(f"Skill is not authorized for role {role}: {name}")
            if RUNTIME not in entry["compatibility"]:
                raise FactoryError(f"Incompatible skill: {name}")
            if not set(entry["prerequisites"]) <= tech:
                raise FactoryError(f"Unmet prerequisites: {name}")
            if name not in chosen:
                chosen.add(name)
                for dependency in entry["dependencies"]:
                    include(dependency)

        for name, entry in sorted(snapshot.entries.items()):
            if (
                name in approved
                and role in entry["roles"]
                and ("*" in entry["technologies"] or tech.intersection(entry["technologies"]))
                and ("*" in entry["tasks"] or set(tasks).intersection(entry["tasks"]))
            ):
                include(name)
        for name in required.get(role, []):
            include(name)
        if not chosen:
            raise FactoryError(f"No approved skills apply to role {role}")
        assignments[role] = sorted(chosen)
    return assignments
