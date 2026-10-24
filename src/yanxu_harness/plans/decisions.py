"""Pure Plan state-transition rules; persistence and authorization stay elsewhere."""

from __future__ import annotations

from .types import PlanRefused, PlanStatus

_TRANSITIONS: dict[PlanStatus, frozenset[PlanStatus]] = {
    PlanStatus.DRAFT: frozenset({PlanStatus.REVIEWING, PlanStatus.CANCELLING}),
    PlanStatus.REVIEWING: frozenset({PlanStatus.APPROVED, PlanStatus.DRAFT, PlanStatus.CANCELLING}),
    PlanStatus.APPROVED: frozenset({PlanStatus.EXECUTING, PlanStatus.PAUSED, PlanStatus.REPLANNING, PlanStatus.CANCELLING, PlanStatus.COMPLETED, PlanStatus.FAILED}),
    PlanStatus.EXECUTING: frozenset({PlanStatus.PAUSED, PlanStatus.REPLANNING, PlanStatus.CANCELLING, PlanStatus.COMPLETED, PlanStatus.FAILED}),
    PlanStatus.PAUSED: frozenset({PlanStatus.APPROVED, PlanStatus.EXECUTING, PlanStatus.REPLANNING, PlanStatus.CANCELLING, PlanStatus.FAILED}),
    PlanStatus.REPLANNING: frozenset({PlanStatus.DRAFT, PlanStatus.CANCELLING, PlanStatus.FAILED}),
    PlanStatus.CANCELLING: frozenset({PlanStatus.CANCELLED, PlanStatus.COMPLETED, PlanStatus.FAILED}),
    PlanStatus.COMPLETED: frozenset(),
    PlanStatus.FAILED: frozenset(),
    PlanStatus.CANCELLED: frozenset(),
}


def transition_plan(
    current: PlanStatus | str,
    target: PlanStatus | str,
    *,
    quiescent: bool = False,
    all_steps_accepted: bool = False,
    plan_acceptance_passed: bool = False,
    active_bindings: bool = False,
    unresolved_unknown: bool = False,
) -> PlanStatus:
    """Validate one requested transition against committed facts supplied by the caller."""
    try:
        source = PlanStatus(current)
        destination = PlanStatus(target)
    except ValueError as exc:
        raise PlanRefused("invalid_transition", "unknown Plan status") from exc
    if destination not in _TRANSITIONS[source]:
        raise PlanRefused(
            "invalid_transition",
            f"Plan cannot move from {source.value!r} to {destination.value!r}",
        )
    if source is PlanStatus.REPLANNING and destination is PlanStatus.DRAFT and not quiescent:
        raise PlanRefused("active_binding", "old revision has not reached the replan quiescence boundary")
    if destination is PlanStatus.COMPLETED:
        if not all_steps_accepted or not plan_acceptance_passed:
            raise PlanRefused("incomplete_plan", "all steps and Plan acceptance criteria must pass")
        if active_bindings:
            raise PlanRefused("active_binding", "Plan still has active Step bindings")
        if unresolved_unknown:
            raise PlanRefused("operation_unknown", "Plan still has unresolved operations")
    if destination in {PlanStatus.CANCELLED, PlanStatus.FAILED}:
        if active_bindings:
            raise PlanRefused("active_binding", f"Plan still has active Step bindings before {destination.value}")
        if unresolved_unknown:
            raise PlanRefused("operation_unknown", f"Plan has unresolved operations before {destination.value}")
    return destination


__all__ = ["transition_plan"]
