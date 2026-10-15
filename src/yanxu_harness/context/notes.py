"""C4: a bounded, source-attributed view of the task, not a second memory.

The plan's section 7.2 asks for one thing from this module: when a long run needs a *task*
view, it must be possible to see what the task is, what was decided, what is done and what
is blocked — **with the provenance of each statement**, so nothing the model inferred can
quietly become a completed fact, a constraint, an approval or a permission.

Hence the shape here:

* every entry carries a :class:`NoteSource` (the host said it, the user said it, a tool
  result proves it, or the model inferred it) and the evidence ids behind it;
* :meth:`TaskNotes.verify` **downgrades** an entry whose evidence is no longer available —
  a claim about a file version stops being a fact when that version is gone, and it is
  never upgraded back by being repeated;
* :meth:`TaskNotes.render` is bounded and labelled, and it states plainly that the notes
  grant nothing;
* a later user instruction can replace the goal (:meth:`TaskNotes.with_goal`), because a
  pinned old goal must not outrank what the user just said.

This is not a scheduler, an authorisation service or a completion record: it holds text a
host chose to keep, and it is wrong on purpose about nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from ..errors import ContractError

__all__ = ["NoteSource", "TaskNote", "TaskNotes"]


class NoteSource(str, Enum):
    """Who is responsible for a statement. The distinction is the point."""

    #: The host deployment configured it (a system constraint, a policy summary).
    HOST = "host"
    #: The user said it in the conversation.
    USER = "user"
    #: A tool result or a receipt proves it; ``evidence`` must name them.
    EVIDENCE = "evidence"
    #: The model inferred or suggested it. Never a fact, never a permission.
    MODEL = "model"


#: Sources whose statements may be treated as established facts.
EVIDENCE_SOURCES = frozenset({NoteSource.HOST, NoteSource.USER, NoteSource.EVIDENCE})

#: Sources that must never be presented as facts, however often they are repeated.
INFERRED_SOURCES = frozenset({NoteSource.MODEL})


@dataclass(frozen=True)
class TaskNote:
    """One statement about the task, with who said it and what backs it."""

    text: str
    source: NoteSource
    #: Ids of the things that back this statement: a tool call id, an artifact id, a message
    #: fingerprint, an approval binding key. Empty means "asserted, not proven".
    evidence: tuple[str, ...] = ()
    #: Set by :meth:`TaskNotes.verify` when the evidence is no longer available.
    unverified: bool = False
    unverified_reason: str = ""

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ContractError("a task note must have text")
        if self.source is NoteSource.EVIDENCE and not self.evidence:
            raise ContractError(
                "a note whose source is a tool result must name the evidence that backs it; "
                "'the model saw it somewhere' is not evidence"
            )

    @property
    def factual(self) -> bool:
        return self.source in EVIDENCE_SOURCES and not self.unverified

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "text": self.text,
            "source": self.source.value,
            "evidence": list(self.evidence),
            "factual": self.factual,
            "unverified_reason": self.unverified_reason,
        }


@dataclass(frozen=True)
class TaskNotes:
    """A bounded, source-attributed task view. Derived data: it owns no state of its own."""

    #: The current goal. Replacing it is how a new user instruction takes effect.
    goal: TaskNote | None = None
    constraints: tuple[TaskNote, ...] = ()
    done: tuple[TaskNote, ...] = ()
    blockers: tuple[TaskNote, ...] = ()
    next_steps: tuple[TaskNote, ...] = ()
    #: Bumped by the host when the inputs it was built from changed; a view is only as
    #: fresh as this, and a re-render never silently re-verifies anything.
    input_version: str = ""

    # -- building -------------------------------------------------------

    def with_goal(self, text: str, *, source: NoteSource, evidence: Sequence[str] = ()) -> "TaskNotes":
        """Replace the goal. A user instruction replaces an older one, whatever pinned it."""
        return replace(self, goal=TaskNote(text=text, source=source, evidence=tuple(evidence)))

    def add(self, section: str, note: TaskNote) -> "TaskNotes":
        if section not in ("constraints", "done", "blockers", "next_steps"):
            raise ContractError(
                f"{section!r} is not a task-note section; use constraints/done/blockers/next_steps"
            )
        return replace(self, **{section: (*getattr(self, section), note)})

    # -- verification ---------------------------------------------------

    def verify(self, available_evidence: Iterable[str]) -> "TaskNotes":
        """Downgrade every entry whose evidence is not in ``available_evidence``.

        Only *evidence-backed* entries can be verified, and an entry with no evidence at all
        is left as it was: it was never a fact, and calling it unverified would be inventing
        a distinction the host did not make. A claim about a file version that no longer
        exists, however, stops being a fact here — which is exactly the plan's requirement
        that a stale file fragment or summary is not a current version.
        """
        available = {str(item) for item in available_evidence}
        return replace(
            self,
            constraints=tuple(self._verify(note, available) for note in self.constraints),
            done=tuple(self._verify(note, available) for note in self.done),
            blockers=tuple(self._verify(note, available) for note in self.blockers),
            next_steps=tuple(self._verify(note, available) for note in self.next_steps),
            goal=None if self.goal is None else self._verify(self.goal, available),
        )

    @staticmethod
    def _verify(note: TaskNote, available: set[str]) -> TaskNote:
        if not note.evidence:
            return note
        missing = [item for item in note.evidence if item not in available]
        if not missing:
            return replace(note, unverified=False, unverified_reason="")
        return replace(
            note,
            unverified=True,
            unverified_reason=(
                "the evidence behind this statement is no longer available: "
                + ", ".join(missing)
            ),
        )

    # -- rendering ------------------------------------------------------

    def render(self, *, max_chars: int = 4000) -> str:
        """A bounded, labelled view. What it says about itself is part of the contract."""
        if max_chars <= 0:
            raise ContractError("max_chars must be positive")
        lines: list[str] = ["[task notes — derived, bounded, and granting nothing]"]
        if self.input_version:
            lines.append(f"[built from input version {self.input_version}]")
        if self.goal is not None:
            lines.append(f"GOAL ({self.goal.source.value}): {self.goal.text}")
        for title, section in (
            ("CONSTRAINTS", self.constraints),
            ("DONE", self.done),
            ("BLOCKED", self.blockers),
            ("NEXT", self.next_steps),
        ):
            if not section:
                continue
            lines.append(f"{title}:")
            for note in section:
                if note.source in INFERRED_SOURCES:
                    marker = "suggested"
                elif note.unverified:
                    marker = f"UNVERIFIED — {note.unverified_reason}"
                elif note.evidence:
                    marker = f"evidence: {', '.join(note.evidence)}"
                else:
                    marker = "asserted"
                lines.append(f"  - {note.text} [{note.source.value}, {marker}]")
        lines.append(
            "[no entry here is an approval, a permission, an authorisation or a completion "
            "record: those live in the approval and operation ledgers]"
        )
        rendered = "\n".join(lines)
        if len(rendered) <= max_chars:
            return rendered
        return (
            rendered[: max_chars - 60].rstrip()
            + "\n[task notes truncated: more entries exist than were shown]"
        )

    # -- reading --------------------------------------------------------

    @property
    def unverified(self) -> tuple[TaskNote, ...]:
        sections = (*self.constraints, *self.done, *self.blockers, *self.next_steps)
        return tuple(note for note in sections if note.unverified)

    @property
    def inferred(self) -> tuple[TaskNote, ...]:
        sections = (*self.constraints, *self.done, *self.blockers, *self.next_steps)
        return tuple(note for note in sections if note.source in INFERRED_SOURCES)

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "input_version": self.input_version,
            "goal": None if self.goal is None else self.goal.as_dict(),
            "constraints": [note.as_dict() for note in self.constraints],
            "done": [note.as_dict() for note in self.done],
            "blockers": [note.as_dict() for note in self.blockers],
            "next_steps": [note.as_dict() for note in self.next_steps],
        }

    @classmethod
    def empty(cls, *, input_version: str = "") -> "TaskNotes":
        return cls(input_version=input_version)


@dataclass
class NoteLedger:
    """A tiny accumulator a host can drive while it runs: text in, bounded view out.

    It exists because the alternative — a host appending raw strings to a list and hoping
    the model treats them carefully — is how "the model said the tests pass" becomes a fact
    in a context window. Nothing here writes to a store or decides anything.
    """

    notes: TaskNotes = field(default_factory=TaskNotes.empty)

    def goal(self, text: str, *, source: NoteSource = NoteSource.USER, evidence: Sequence[str] = ()) -> None:
        self.notes = self.notes.with_goal(text, source=source, evidence=evidence)

    def record(self, section: str, text: str, *, source: NoteSource, evidence: Sequence[str] = ()) -> None:
        self.notes = self.notes.add(section, TaskNote(text=text, source=source, evidence=tuple(evidence)))

    def view(self, *, available_evidence: Iterable[str] | None = None, max_chars: int = 4000) -> str:
        notes = self.notes if available_evidence is None else self.notes.verify(available_evidence)
        return notes.render(max_chars=max_chars)
