"""Pure, deterministic validation and readiness rules for a Plan DAG."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from ..errors import ContractError
from .types import PLAN_MAX_EDGES, PLAN_MAX_STEPS, PlanRefused, StepOutcome


@dataclass(frozen=True)
class StepReadiness:
    step_id: str
    ready: bool
    reason: str = ""
    blocked_by: tuple[str, ...] = ()


def validate_graph(steps: Sequence[object], *, max_steps: int = PLAN_MAX_STEPS) -> None:
    """Refuse duplicate/missing/self/cyclic edges and enforce graph bounds."""
    if not 1 <= len(steps) <= max_steps:
        raise PlanRefused("invalid_definition", f"a graph must contain 1–{max_steps} steps")
    ids = [str(getattr(step, "step_id", "")) for step in steps]
    if any(not item for item in ids):
        raise PlanRefused("invalid_definition", "every step must have a non-empty step_id")
    if len(set(ids)) != len(ids):
        raise PlanRefused("invalid_definition", "step IDs must be unique within a revision")
    known = set(ids)
    edges = 0
    adjacency: dict[str, tuple[str, ...]] = {}
    for step in steps:
        step_id = str(getattr(step, "step_id"))
        dependencies = tuple(getattr(step, "depends_on", ()))
        edges += len(dependencies)
        if len(set(dependencies)) != len(dependencies):
            raise PlanRefused("invalid_definition", f"step {step_id!r} has duplicate dependencies")
        if step_id in dependencies:
            raise PlanRefused("invalid_definition", f"step {step_id!r} depends on itself")
        missing = sorted(set(dependencies) - known)
        if missing:
            raise PlanRefused(
                "invalid_definition",
                f"step {step_id!r} depends on missing step(s): {', '.join(missing)}",
            )
        adjacency[step_id] = tuple(sorted(dependencies))
    if edges > PLAN_MAX_EDGES:
        raise PlanRefused("invalid_definition", f"graph exceeds {PLAN_MAX_EDGES} dependency edges")
    _topological_order(ids, adjacency)


def topological_order(steps: Iterable[object]) -> tuple[str, ...]:
    ordered_steps = tuple(steps)
    validate_graph(ordered_steps)
    adjacency = {
        str(getattr(step, "step_id")): tuple(sorted(getattr(step, "depends_on", ())))
        for step in ordered_steps
    }
    return _topological_order(tuple(adjacency), adjacency)


def _topological_order(ids: Iterable[str], dependencies: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    dependents: dict[str, list[str]] = {step_id: [] for step_id in ids}
    indegree: dict[str, int] = {}
    for step_id, required in dependencies.items():
        indegree[step_id] = len(required)
        for dependency in required:
            dependents[dependency].append(step_id)
    ready = [step_id for step_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        current = heapq.heappop(ready)
        order.append(current)
        for dependent in sorted(dependents[current]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(order) != len(indegree):
        cyclic = sorted(step_id for step_id, count in indegree.items() if count > 0)
        raise PlanRefused("invalid_definition", f"dependency graph contains a cycle: {', '.join(cyclic)}")
    return tuple(order)


def readiness_for_step(
    step_id: str,
    steps: Sequence[object],
    outcomes: Mapping[str, StepOutcome | str],
) -> StepReadiness:
    """Explain whether one step's all-accepted prerequisites are satisfied."""
    matches = [step for step in steps if getattr(step, "step_id", None) == step_id]
    if not matches:
        raise ContractError(f"unknown step {step_id!r}")
    step = matches[0]
    dependencies = tuple(sorted(getattr(step, "depends_on", ())))
    blocked: list[str] = []
    outcomes_by_id: dict[str, StepOutcome] = {}
    for dependency in dependencies:
        try:
            outcome = StepOutcome(outcomes.get(dependency, StepOutcome.PENDING))
        except ValueError as exc:
            raise ContractError(f"unknown outcome for dependency {dependency!r}") from exc
        outcomes_by_id[dependency] = outcome
        if outcome is not StepOutcome.ACCEPTED:
            blocked.append(dependency)
    if not blocked:
        return StepReadiness(step_id=step_id, ready=True)
    blocked_outcomes = tuple(outcomes_by_id[dependency] for dependency in blocked)
    if StepOutcome.REJECTED in blocked_outcomes:
        reason = "dependency_rejected"
    elif any(item in {StepOutcome.FAILED, StepOutcome.CANCELLED} for item in blocked_outcomes):
        reason = "dependency_failed"
    else:
        reason = "dependency_pending"
    return StepReadiness(step_id=step_id, ready=False, reason=reason, blocked_by=tuple(blocked))


__all__ = ["StepReadiness", "readiness_for_step", "topological_order", "validate_graph"]
