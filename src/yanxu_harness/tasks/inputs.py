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
  ``applied`` in the same transaction as the state that carries it;
* it does not scan applied history or start from ``MAX(applied)``. Each poll asks the store for
  the *still-claimable* inputs of the kind the site expects (:meth:`CoordinationStore\
  .poll_pending_inputs`), because a watermark is a high-water mark, not a contiguous consumed
  prefix — reading by watermark is how an older follow-up was skipped once a newer steering input
  advanced it (defect DIO2-I1).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..core.execution import InputPoll
from ..errors import ContractError
from .ports import require_input_polling
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
        # Refused before the first read: a store without a bounded pending-input query would have
        # to be driven by applied history plus a guessed watermark, which is the defect this
        # module exists to remove. There is deliberately no ``getattr`` fallback (plan §5.1).
        require_input_polling(control)
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
        # Read the *still-claimable* inputs of the kind this site expects, by state, rather than
        # scanning applied history from a watermark: an applied watermark is a high-water mark,
        # not a contiguous consumed prefix, so a newer steering input can be applied while an
        # older follow-up is still waiting (defect DIO2-I1).
        wanted = str(getattr(kind, "value", "") or "")
        read = await self._read(kind=self._tasks_kind(wanted))
        records = list(read.candidates)
        revision = int(read.revision)
        # The after-inner site reads the follow-up that would restart the inner work; a steering
        # input is only a fallback when no follow-up waits, and it is read at the same logical
        # boundary (the second read's revision can only be newer, never older).
        if wanted == InputKind.FOLLOW_UP.value and not records:
            fallback = await self._read(kind=InputKind.STEERING)
            records = list(fallback.candidates)
            revision = max(revision, int(fallback.revision))
        selection = select_inputs(records, limit=self._limit)
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
        return InputPoll(messages=messages, input_refs=refs, inbox_revision=revision)

    async def _read(self, *, kind: Any = None) -> Any:
        return await self._control.poll_pending_inputs(
            self._task_id, kind=kind, limit=self._limit
        )

    @staticmethod
    def _tasks_kind(wanted: str) -> InputKind | None:
        """Map the poll site's kind onto the control vocabulary (``None`` means any)."""
        if wanted == InputKind.FOLLOW_UP.value:
            return InputKind.FOLLOW_UP
        if wanted == InputKind.STEERING.value:
            return InputKind.STEERING
        return None

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
