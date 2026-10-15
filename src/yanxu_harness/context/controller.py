"""C3: prepare one model request, and say what the preparation produced.

The controller is the piece the plan calls for in section 4.1: it composes the C2
projector with the C1 budget and turns one ``RequestPreparation`` payload into

* the **view** the request will be built from (never the authoritative transcript),
* the **facts** about that view — scope, source head, fingerprints, estimator identity,
  what was kept and dropped, why — as plain, versioned data, and
* a **refusal** when the minimum required context does not fit, which is a result rather
  than an exception so the loop can record a budget stop instead of a model failure.

Three boundaries are deliberate:

* it is pure with respect to the transcript: it reads it, never rewrites it, and the
  caller keeps it as the single session source;
* it is synchronous and does no I/O, so it can run inside the loop's preparation step and
  its result can be committed before anything is dispatched (an optional summary call
  belongs to C5, and it will have to happen before this step, not after it);
* the facts it produces carry ``context_schema_version`` so a reader refuses a projection
  it cannot interpret instead of guessing what a view means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..core.execution import CONTEXT_STATE_SCHEMA_VERSION
from ..core.outcome import CONTEXT_BUDGET_STOP_CODE
from ..errors import BudgetExceeded, ContractError
from .measurement import RequestBudget, TokenEstimator, UnknownPolicy
from .projection import FragmentProjector, ProjectionResult, transcript_fingerprint

__all__ = ["CONTEXT_STATE_SCHEMA_VERSION", "ContextController", "PreparationOutcome"]


@dataclass(frozen=True)
class PreparationOutcome:
    """What one preparation produced: a view with its facts, or a refusal."""

    view: tuple[Any, ...] = ()
    context_state: Mapping[str, Any] | None = None
    refused: Mapping[str, Any] | None = None

    @property
    def outbound(self) -> bool:
        """True when this outcome means a request may be sent."""
        return self.refused is None

    def as_mapping(self) -> Mapping[str, Any]:
        """The shape the loop's ``RequestPrepared`` event expects."""
        return {
            "view": self.view,
            "context_state": None if self.context_state is None else dict(self.context_state),
            "refused": None if self.refused is None else dict(self.refused),
        }


@dataclass
class ContextController:
    """Measure one request, project it, and explain the result.

    ``converter`` must be the same conversion the loop uses for the wire request
    (``AgentLoopConfig.convert_to_llm`` or its default), so the measurement describes the
    request that is actually sent. That is the reason the loop refuses to combine this
    with ``transform_context``: two transformations over one request would make the
    measured request and the sent one different documents.
    """

    projector: FragmentProjector
    budget: RequestBudget
    #: Messages the host requires in every view (a task constraint it wants kept).
    mandatory: Sequence[Any] = field(default_factory=tuple)

    @classmethod
    def compose(
        cls,
        *,
        estimator: TokenEstimator,
        converter: Any,
        window: Any,
        max_payload_bytes: int | None = None,
        on_unknown: UnknownPolicy = UnknownPolicy.PROCEED,
        mandatory: Sequence[Any] = (),
        policy_version: str = "1",
        cache_limit: int = 256,
    ) -> "ContextController":
        """Build a controller from the ports it needs, instead of wiring three objects."""
        return cls(
            projector=FragmentProjector(
                estimator=estimator,
                convert=converter,
                policy_version=policy_version,
                cache_limit=cache_limit,
            ),
            budget=RequestBudget(
                window=window, max_payload_bytes=max_payload_bytes, on_unknown=on_unknown
            ),
            mandatory=tuple(mandatory),
        )

    def prepare(self, payload: Any, *, scope: str) -> Mapping[str, Any]:
        """Prepare the request for one turn. Returns the loop's prepared mapping.

        ``payload`` is the core's ``RequestPreparation``: the transcript after the
        tool-set delta, the model, the turn number and the cancellation token. The scope
        comes from whoever owns the run identity.
        """
        messages = tuple(getattr(payload, "messages", ()) or ())
        if not scope:
            raise ContractError("context preparation needs the scope it is isolated by")
        model = getattr(payload, "model", None)
        request_cap = _declared_output_cap(model)
        capacity, reserve, reserve_source = self.budget.capacity_for(request_cap)
        head = transcript_fingerprint(messages)
        try:
            projection = self.projector.plan(
                messages,
                scope=scope,
                source_head=head,
                capacity=capacity,
                mandatory=self.mandatory,
            )
        except BudgetExceeded as refusal:
            return self._refused(
                scope=scope,
                head=head,
                detail=str(refusal) or "the minimum required context does not fit",
                capacity=capacity,
                estimated=None,
                reserve=reserve,
                reserve_source=reserve_source,
            ).as_mapping()

        report = self.budget.assess(
            projection.estimate, request_max_output_tokens=request_cap
        )
        if report.fits is False:
            return self._refused(
                scope=scope,
                head=head,
                detail=report.reason,
                capacity=capacity,
                estimated=projection.estimate.tokens,
                reserve=reserve,
                reserve_source=reserve_source,
            ).as_mapping()
        if report.fits is None and self.budget.on_unknown is UnknownPolicy.REFUSE:
            return self._refused(
                scope=scope,
                head=head,
                detail=f"the request could not be proven to fit: {report.reason}",
                capacity=capacity,
                estimated=projection.estimate.tokens,
                reserve=reserve,
                reserve_source=reserve_source,
            ).as_mapping()

        return PreparationOutcome(
            view=projection.messages,
            context_state=self._facts(
                scope=scope,
                head=head,
                projection=projection,
                capacity=capacity,
                reserve=reserve,
                reserve_source=reserve_source,
            ),
        ).as_mapping()

    # -- the facts ------------------------------------------------------

    def _facts(
        self,
        *,
        scope: str,
        head: str,
        projection: ProjectionResult,
        capacity: int | None,
        reserve: int,
        reserve_source: str,
    ) -> Mapping[str, Any]:
        estimate = projection.estimate
        return {
            "context_schema_version": CONTEXT_STATE_SCHEMA_VERSION,
            "scope": scope,
            "policy_version": projection.policy_version,
            "source_head": projection.source_head,
            "source_fingerprint": projection.source_fingerprint,
            "request_fingerprint": projection.request_fingerprint,
            "effective_state_fingerprint": projection.effective_state_fingerprint,
            "estimator_id": estimate.estimator_id,
            "estimator_version": estimate.estimator_version,
            "estimator_method": estimate.method,
            "estimate_exact": estimate.exact,
            "estimated_input_tokens": estimate.tokens,
            "capacity": capacity,
            "output_reserve": reserve,
            "output_reserve_source": reserve_source,
            "kept_fragments": list(projection.kept_fragments),
            "removed_messages": projection.removed_messages,
            "dropped": [
                {
                    "index": fragment.index,
                    "kind": fragment.kind,
                    "positions": list(fragment.positions),
                    "reason": fragment.reason,
                }
                for fragment in projection.dropped
            ],
            "baseline_synthesized": projection.baseline_synthesized,
            "open_call_ids": list(projection.open_call_ids),
            "refused": None,
        }

    def _refused(
        self,
        *,
        scope: str,
        head: str,
        detail: str,
        capacity: int | None,
        estimated: int | None,
        reserve: int,
        reserve_source: str,
    ) -> PreparationOutcome:
        """A refusal is recorded with the same provenance a prepared view gets.

        A host that reads "why was no request made" must not have to infer it from a
        status code: the scope, the source it refused to cut, the capacity and the reason
        are all part of the committed state.
        """
        return PreparationOutcome(
            view=(),
            context_state={
                "context_schema_version": CONTEXT_STATE_SCHEMA_VERSION,
                "scope": scope,
                "policy_version": self.projector.policy_version,
                "source_head": head,
                "source_fingerprint": head,
                "request_fingerprint": None,
                "effective_state_fingerprint": None,
                "estimator_id": self.projector.estimator.estimator_id,
                "estimator_version": self.projector.estimator.estimator_version,
                "estimator_method": self.projector.estimator.method,
                "estimate_exact": False,
                "estimated_input_tokens": estimated,
                "capacity": capacity,
                "output_reserve": reserve,
                "output_reserve_source": reserve_source,
                "kept_fragments": [],
                "removed_messages": 0,
                "dropped": [],
                "baseline_synthesized": False,
                "open_call_ids": [],
                "refused": {
                    "error_code": CONTEXT_BUDGET_STOP_CODE,
                    "detail": detail,
                },
            },
            refused={"error_code": CONTEXT_BUDGET_STOP_CODE, "detail": detail},
        )


def _declared_output_cap(model: Any) -> int | None:
    """The request's own output cap, resolved the same way the wire path resolves it.

    ``to_provider_request`` reads the model's declaration before falling back to the
    options; the measurement must deduct the same number the provider will honour, and a
    model that declares nothing keeps the reserve *unknown* rather than zero.
    """
    declared = getattr(model, "max_output_tokens", None)
    return declared if type(declared) is int and declared > 0 else None
