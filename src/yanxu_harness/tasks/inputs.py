"""Reading a durable inbox at the runner's poll sites, without confirming anything.

The runner polls a queue at four sites (before the first request, after a preparation, at a turn
boundary, and after the inner work). A deployment with persistent input supplies one callable that
turns those polls into :class:`~yanxu_harness.core.execution.InputPoll` values: the messages to
integrate **and** the identity of the admitted inputs they came from.

What this reader deliberately does not do:

* it does not *confirm* anything. Reading candidates is not applying them: the confirmation is the
  commit of the state that integrates them, in the same transaction (plan §7.2). A process that
  dies between the read and the commit leaves the input claimable, which is the whole point of not
  acknowledging on read;
* it does not drain destructively. Every poll is a plain read, so a retry reads the same set;
* it does not read control inputs as conversation. ``cancel_request``, ``approval_decision`` and
  ``child_completed`` change authority or lifecycle and are handled at the boundaries that own
  them, never as text handed to the model;
* it does not re-take what is already applied. The control store's own state decides: an input is
  a candidate while it is ``pending`` or ``claimed``, and the commit that integrates it marks it
  ``applied`` in the same transaction as the state that carries it.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..core.execution import InputPoll
from ..errors import ContractError
from .types import INPUT_ORDER, AdmissionOutcome, InputAdmission, InputContent, InputKind, InputState, select_inputs

__all__ = ["PersistentInputReader", "input_message_text"]


def input_message_text(record: Any) -> str:
    """The text one admitted input contributes to the conversation (or refusal)."""
    content = getattr(record, "content", None)
    text = str(getattr(content, "text", "") or "")
    if not text:
        raise ContractError(
            f"input {getattr(record, 'input_id', '?')!r} of kind "
            f"{getattr(getattr(content, 'kind', None), 'value', '?')} carries no text; a control "
            "entry or an empty payload is not conversation and must not be read as if it were"
        )
    return text


class PersistentInputReader:
    """A poll callable for deployments whose input lives in the control store.

    The reader is *not* the owner of the input: the control store is. A reader that also wrote
    would be a second source of truth about what was consumed, which is exactly the split this
    package refuses.
    """

    def __init__(self, control: Any, task_id: str, *, limit: int = 8) -> None:
        if control is None:
            raise ContractError("a persistent input reader needs a control store")
        if not task_id:
            raise ContractError("a persistent input reader needs the task it reads for")
        if limit <= 0:
            raise ContractError("the input read limit must be positive")
        self._control = control
        self._task_id = task_id
        self._limit = limit

    @property
    def task_id(self) -> str:
        return self._task_id

    async def __call__(self, site: Any = None, kind: Any = None) -> InputPoll:
        # Read from the watermark forward, not from sequence -1: once the task has more than one
        # page of history, a window anchored at the start never reaches a new input.
        watermark_reader = getattr(self._control, "read_applied_watermark", None)
        watermark = int(await watermark_reader(self._task_id)) if callable(watermark_reader) else -1
        records = list(
            await self._control.read_inputs(
                self._task_id, after_sequence=max(-1, watermark), limit=200
            )
        )
        candidates = [
            record
            for record in records
            if record.state in {InputState.PENDING, InputState.CLAIMED}
            and record.kind is not InputKind.INITIAL
            and INPUT_ORDER[record.kind] != "control"
        ]
        # The poll site decides *which kind* may be taken: steering is read before a request
        # (INITIAL / TURN_BOUNDARY / AFTER_PREPARE), follow-up only at the after-inner boundary.
        # A reader that ignores the site merges a follow-up into the opening turn, which changes
        # when it is consumed. At AFTER_INNER a steering may still be read — it is the input the
        # next request exists for — but only when no follow-up is waiting, because the follow-up
        # is the kind that restarts the inner work there.
        wanted = str(getattr(kind, "value", "") or "")
        if wanted == InputKind.FOLLOW_UP.value:
            follow_ups = [record for record in candidates if record.kind is InputKind.FOLLOW_UP]
            candidates = (
                follow_ups
                if follow_ups
                else [record for record in candidates if record.kind is InputKind.STEERING]
            )
        elif wanted == InputKind.STEERING.value:
            candidates = [record for record in candidates if record.kind is InputKind.STEERING]
        selection = select_inputs(candidates, limit=self._limit)
        taken = [record for record in selection.taken if str(record.content.text or "")]
        from ..core.types import UserMessage

        messages = tuple(UserMessage(content=input_message_text(record)) for record in taken)
        refs: tuple[Mapping[str, Any], ...] = tuple(
            {
                "task_id": record.task_id,
                "input_id": record.input_id,
                "sequence": record.sequence,
                "kind": record.kind.value,
            }
            for record in taken
        )
        revision = await self._control.read_inbox_revision(self._task_id)
        return InputPoll(messages=messages, input_refs=refs, inbox_revision=int(revision))

    async def initial_reference(self) -> Mapping[str, Any] | None:
        """The admitted ``initial`` input, if the run has not integrated it yet.

        The initial input is the run's opening instruction: it is the *seed* of the run rather than
        a poll result, so the harness confirms it in the transaction that creates the run — which
        is what makes "the run exists, so its opening instruction was applied" true.
        """
        records = list(await self._control.read_inputs(self._task_id, after_sequence=-1, limit=200))
        for record in records:
            if record.kind is InputKind.INITIAL and record.state is InputState.PENDING:
                return {
                    "task_id": record.task_id,
                    "input_id": record.input_id,
                    "sequence": record.sequence,
                    "kind": record.kind.value,
                }
        return None

    async def admit(
        self,
        input_id: str,
        content: InputContent,
        *,
        namespace: str,
        session_id: str,
        source: str = "host",
    ) -> AdmissionOutcome:
        """Admit one input through the control store; the caller gets the outcome, not a promise."""
        result = await self._control.admit_input(
            InputAdmission(
                namespace=namespace,
                session_id=session_id,
                task_id=self._task_id,
                input_id=input_id,
                content=content,
                source=source,
            )
        )
        return result.outcome

    def claimable_kinds(self) -> Sequence[str]:
        """The kinds this reader may hand to the model, for documentation and tests."""
        return tuple(
            kind.value
            for kind in InputKind
            if kind is not InputKind.INITIAL and INPUT_ORDER[kind] != "control"
        )
