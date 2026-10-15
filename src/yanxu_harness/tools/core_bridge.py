"""The tool → core bridge: a definition the model can call, executed by the gateway.

Core knows one thing about tools: a :class:`~yanxu_harness.core.types.ToolSpec`
with an ``execute(tool_call_id, args, signal, on_update)`` callable that returns
``{content, details, is_error}``. The package's authority path is
:class:`~yanxu_harness.tools.gateway.ToolGateway`, which owns visibility,
validation, policy, approval, intent and receipt. This module is the only place
those two meet, and it is deliberately thin: it builds an invocation from
*server-side* identity, calls the gateway once, and reports what the gateway
concluded.

What it refuses to do:

* **Invent identity.** ``namespace``/``session``/``run`` come from the run context
  the host established; tool arguments can repeat an identity but never establish
  one.
* **Flatten the outcome.** ``executed``, ``replayed``, ``awaiting_approval``,
  ``deferred``, ``denied``, ``invalid_args``, ``failed``, ``cancelled`` and
  ``unknown`` each keep their own name in ``details["status"]``. ``is_error``
  means "the model must not treat this as a completed effect" — it is not a
  synonym for "denied".
* **Write a second ledger.** Execution happens in the gateway's backend; a
  host-owned backend (``ownership == "host"``) keeps owning it, and this adapter
  neither records an intent nor a receipt of its own.
* **Claim a suspension is a finished call.** ``awaiting_approval``/``deferred``/
  ``unknown`` are reported as such and are visible through
  :func:`suspension_reason`; the run-level decision to stop before the next model
  request belongs to the harness, not to a fabricated tool result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..core.types import TextContent, ToolSpec, block_from_json
from ..errors import ContractError
from ..runtime.context import RunContext
from .definition import Idempotency, ToolDefinition
from .gateway import InvocationStatus, ToolGateway, ToolInvocation, ToolInvocationResult

__all__ = [
    "PENDING_STATUSES",
    "GatewayBindings",
    "core_tool",
    "result_status",
    "suspension_reason",
    "tool_result_from_invocation",
]

#: Statuses that mean "the run must not ask the model again until something
#: outside this call changes": a human decision, or evidence about an effect the
#: harness cannot prove.
PENDING_STATUSES = frozenset(
    {
        InvocationStatus.AWAITING_APPROVAL,
        InvocationStatus.DEFERRED,
        InvocationStatus.UNKNOWN,
    }
)

#: Statuses whose result content is a real tool result the model can use.
_SETTLED_SUCCESS = frozenset({InvocationStatus.EXECUTED, InvocationStatus.REPLAYED})


@dataclass(frozen=True)
class GatewayBindings:
    """One run's gateway plus the server-side identity its calls must carry.

    ``context`` may be a plain :class:`~yanxu_harness.runtime.context.RunContext` or a
    zero-argument callable returning one. The callable form is what a *resumed* run
    needs: the identity it dispatches under has a new ``attempt_id``, and the tool
    adapter must pick it up at call time rather than from a context captured when
    the harness was constructed.
    """

    gateway: ToolGateway
    context: RunContext | Callable[[], RunContext]
    #: Server-side version of the object a call would change. The model may repeat
    #: a version in its arguments; this callback is what an approval binds to.
    target_version: Callable[[str, Mapping[str, Any]], str | None] | None = None
    #: Explicit operation id for a call, when the host has one (resumption). When
    #: absent the gateway derives it from run id and call id, which keeps two
    #: namespaces from colliding on the same call id.
    operation_id: Callable[[str], str | None] | None = None

    def resolve_context(self) -> RunContext:
        """The run context to dispatch under, resolved at call time."""
        return self.context() if callable(self.context) else self.context

    def invocation(
        self,
        definition: ToolDefinition,
        call_id: str,
        arguments: Mapping[str, Any],
        *,
        model_action_id: str = "",
    ) -> ToolInvocation:
        identity = self.resolve_context().identity
        return ToolInvocation(
            call_id=call_id,
            namespace=identity.namespace,
            tool_name=definition.name,
            arguments=dict(arguments),
            target_version=self.target_version(call_id, arguments) if self.target_version else None,
            session_id=identity.session_id,
            run_id=identity.run_id,
            operation_id=self.operation_id(call_id) if self.operation_id else None,
            # The batch reference is passed in by the driver that is executing the batch;
            # it is not read from shared mutable state, which is what keeps two batches
            # from sharing an identity by accident.
            model_action_id=model_action_id,
        )


def core_tool(definition: ToolDefinition, bindings: GatewayBindings, *, label: str = "") -> ToolSpec:
    """Adapt one :class:`ToolDefinition` into a core tool that runs through the gateway."""

    async def execute(
        call_id: str,
        arguments: Any,
        signal: Any = None,
        on_update: Any = None,
        model_action_id: str = "",
    ) -> Mapping[str, Any]:
        invocation = bindings.invocation(
            definition, call_id, arguments or {}, model_action_id=model_action_id
        )
        # The tool-call child context inherits the principal and the cancellation
        # token unchanged; it can only add the call id.
        context = bindings.resolve_context().for_tool_call(call_id)
        result = await bindings.gateway.invoke(invocation, context)
        return tool_result_from_invocation(result)

    return ToolSpec(
        name=definition.name,
        description=definition.description,
        parameters=dict(definition.schema() or {}),
        label=label,
        execute=execute,
        execution_mode=definition.execution_mode.value,
        # Core passes `replay` through untouched; the harness uses it to decide
        # whether an operation with an unproven outcome may ever be repeated.
        replay="keyed" if definition.idempotency is Idempotency.KEYED else "never",
    )


def tool_result_from_invocation(result: ToolInvocationResult) -> dict[str, Any]:
    """Project a gateway result into the core tool-result shape.

    ``details`` is the machine-readable half and is where the status survives;
    the text is the model-facing half. Both are always present, so a caller that
    only reads one of them still cannot mistake a refusal for a success.
    """
    status = result.status
    details: dict[str, Any] = {
        "status": status.value,
        "tool": result.tool_name,
        "tool_version": result.tool_version,
        "operation_id": result.operation_id,
    }
    if result.args_hash:
        details["args_hash"] = result.args_hash
    if result.reason:
        details["reason"] = result.reason
    if result.approval is not None:
        details["approval"] = {
            "binding_key": result.approval.binding.key(),
            "status": result.approval.status.value,
            "expires_at": result.approval.binding.expires_at,
        }
    if result.preview is not None:
        details["preview"] = dict(result.preview)
    if result.receipt is not None:
        details["effect_state"] = result.receipt.state.value
        if result.receipt.external_id:
            details["external_id"] = result.receipt.external_id
        if result.receipt.details:
            details["receipt_details"] = dict(result.receipt.details)

    return {
        "content": _content_for(result),
        "details": details,
        "is_error": status not in _SETTLED_SUCCESS,
    }


def _content_for(result: ToolInvocationResult) -> list[Any]:
    if result.status in _SETTLED_SUCCESS and result.receipt is not None:
        return _receipt_content(result.receipt.result)
    # A real TextContent, not a raw dict: the core transcript reads text blocks by
    # attribute, so a dict here would hand the model an empty tool result.
    return [TextContent(text=_refusal_text(result))]


def _receipt_content(payload: Any) -> list[Any]:
    """A handler's result, in the shape the model can read.

    Content blocks pass through unchanged; a plain string or value becomes text,
    because a tool result that the model cannot read is the same as no result.
    """
    if payload is None:
        return []
    if isinstance(payload, Mapping):
        content = payload.get("content")
        if content is None:
            return []
        if isinstance(content, str):
            return [TextContent(text=content)]
        return _as_blocks(list(content))
    if isinstance(payload, str):
        return [TextContent(text=payload)]
    if isinstance(payload, (list, tuple)):
        return _as_blocks(list(payload))
    return [TextContent(text=str(payload))]


def _as_blocks(content: list[Any]) -> list[Any]:
    """Normalise a handler's content list into core blocks.

    Three shapes arrive here: core blocks (from a handler in this process), bare
    strings, and *stored* blocks — plain mappings read back from the operation
    ledger, which is what a replayed call returns. The last one has to be rebuilt,
    or the model would see a result whose text is invisible to it.
    """
    blocks: list[Any] = []
    for item in content:
        if isinstance(item, str):
            blocks.append(TextContent(text=item))
        elif isinstance(item, Mapping) and "type" in item:
            blocks.append(block_from_json(item))
        else:
            blocks.append(item)
    return blocks


def _refusal_text(result: ToolInvocationResult) -> str:
    tool = result.tool_name
    reason = result.reason or result.status.value
    match result.status:
        case InvocationStatus.DENIED:
            return f"{tool}: the call was denied — {reason}"
        case InvocationStatus.INVALID_ARGS:
            return f"{tool}: the arguments were rejected — {reason}. Fix the arguments and call again."
        case InvocationStatus.FAILED:
            return f"{tool}: the call failed — {reason}"
        case InvocationStatus.CANCELLED:
            return f"{tool}: the call was cancelled — {reason}"
        case InvocationStatus.UNKNOWN:
            return (
                f"{tool}: the outcome of operation {result.operation_id} could not be proven — {reason}. "
                "It is not retried automatically; it needs reconciliation with business evidence."
            )
        case InvocationStatus.AWAITING_APPROVAL:
            key = result.approval.binding.key() if result.approval is not None else ""
            return (
                f"{tool}: waiting for an approval decision on operation {result.operation_id} "
                f"(approval {key}). Nothing has been written."
            )
        case InvocationStatus.DEFERRED:
            return f"{tool}: waiting for a human decision — {reason}. Nothing has been written."
    return f"{tool}: {reason}"


def result_status(result: Mapping[str, Any]) -> InvocationStatus | None:
    """Read the status back out of a tool result, refusing an unknown value.

    A status this build does not know is a contract violation, not "probably
    fine": a caller that treats it as success is how a refusal gets lost.
    """
    details = result.get("details")
    if not isinstance(details, Mapping):
        return None
    raw = details.get("status")
    if raw is None:
        return None
    try:
        return InvocationStatus(str(raw))
    except ValueError as exc:
        raise ContractError(f"unknown tool status {raw!r} in a tool result") from exc


def suspension_reason(result: Mapping[str, Any]) -> str | None:
    """Why the run must stop before the next model request, or ``None``.

    ``unknown`` is included on purpose: an effect that may or may not have
    happened cannot be continued past, and it cannot be retried either.
    """
    status = result_status(result)
    if status in PENDING_STATUSES:
        return status.value
    return None
