"""The versioned shapes an evaluation is stated in.

A case is a *question*: which task, on which fixture, checked by which verifier, under which
budgets, with which fault injections allowed. A result is an *answer*: what happened, what was
verified, and what could not be established. Both are frozen dataclasses with explicit
``to_record``/``from_record`` and their own schema version, because a comparison between two
versions is only meaningful if each side says which question it answered.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping

from ..errors import ContractError, VersionRefused

__all__ = [
    "EVAL_SCHEMA_VERSION",
    "EvalCase",
    "EvalCaseResult",
    "EvalOutcome",
    "EvalSuite",
    "FixtureSpec",
    "SUPPORTED_EVAL_SCHEMA_VERSIONS",
    "VerifierOutcome",
]

EVAL_SCHEMA_VERSION = 1
SUPPORTED_EVAL_SCHEMA_VERSIONS = (1,)


class EvalOutcome(str, Enum):
    """How one case run ended, in the only four categories a comparison may mix.

    ``passed``/``failed`` are *task* verdicts from the independent verifier.
    ``infra_error`` and ``invalid_result`` are the framework's own failures and must never be
    counted as the task failing — a harness that cannot start is not a model that cannot
    code, and a result missing its fixture digest cannot be compared with anything.
    """

    PASSED = "passed"
    FAILED = "failed"
    INFRA_ERROR = "infra_error"
    INVALID_RESULT = "invalid_result"
    NOT_RUN = "not_run"
    CANCELLED = "cancelled"

    @property
    def is_task_verdict(self) -> bool:
        return self in {EvalOutcome.PASSED, EvalOutcome.FAILED}


@dataclass(frozen=True)
class FixtureSpec:
    """A frozen fixture: the files, their digests, and the version they belong to.

    The files are carried inline (they are small on purpose) so a result's digest is
    reproducible from the case definition alone, without a checkout of anything.
    """

    name: str
    version: str
    files: Mapping[str, str] = field(default_factory=dict)
    #: A command that proves the fixture is in its initial (broken) state, run by the runner
    #: before the task starts. ``None`` means the fixture needs no such check.
    baseline_command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise ContractError("a fixture needs a name and a version")
        for path, content in self.files.items():
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise ContractError(
                    f"fixture {self.name}: {path!r} must be a relative path inside the workspace"
                )
            if not isinstance(content, str):
                raise ContractError(f"fixture {self.name}: {path!r} must hold text")

    @property
    def digest(self) -> str:
        from ..versioning import fingerprint

        return fingerprint(
            {
                "fixture": self.name,
                "version": self.version,
                "files": {path: content for path, content in sorted(self.files.items())},
            }
        )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "digest": self.digest,
            "files": dict(self.files),
            "baseline_command": list(self.baseline_command),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "FixtureSpec":
        return cls(
            name=str(record.get("name") or ""),
            version=str(record.get("version") or ""),
            files={str(k): str(v) for k, v in (record.get("files") or {}).items()},
            baseline_command=tuple(str(item) for item in record.get("baseline_command") or ()),
        )


@dataclass(frozen=True)
class EvalCase:
    """One frozen task: what to do, where, checked how, under which limits."""

    case_id: str
    version: str
    #: The task as the model receives it, plus a stable reference to the fuller description.
    instructions: str
    instructions_ref: str = ""
    fixture: FixtureSpec | None = None
    #: Tools and policy the deployment offers (names, and whether writes are auto-approved).
    tool_config: Mapping[str, Any] = field(default_factory=dict)
    #: The independent check: a name resolved to a shipped verifier, plus its own digest so a
    #: changed verifier cannot be compared with an older result.
    verifier: str = ""
    verifier_digest: str = ""
    #: Budgets. ``max_runtime_seconds=None`` means no wall-clock limit for the case.
    max_turns: int = 8
    max_runtime_seconds: float | None = None
    #: Which fault injections this case allows. A run may inject only these; an undeclared
    #: fault is a framework error, not a task result.
    allowed_faults: tuple[str, ...] = ()
    #: ``offline`` for the scripted adapter, or a provider model id for a real-model run.
    model: str = "offline"
    repeats: int = 1
    seed: int | None = None
    #: Extra facts the verifier needs (an expected file digest, a reconciliation answer).
    expectations: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.case_id or not self.version:
            raise ContractError("an eval case needs a case_id and a version")
        if not self.instructions:
            raise ContractError(f"{self.case_id}: a case must state the task it asks for")
        if not self.verifier:
            raise ContractError(f"{self.case_id}: a case must name its independent verifier")
        if self.max_turns <= 0:
            raise ContractError(f"{self.case_id}: max_turns must be positive")
        if self.max_runtime_seconds is not None and self.max_runtime_seconds <= 0:
            raise ContractError(f"{self.case_id}: max_runtime_seconds must be positive when set")
        if self.repeats <= 0:
            raise ContractError(f"{self.case_id}: repeats must be positive")
        if self.model != "offline" and self.repeats <= 1:
            raise ContractError(
                f"{self.case_id}: a real-model case must be repeated; a single sample is not a "
                "measurement of a model"
            )

    @property
    def digest(self) -> str:
        from ..versioning import fingerprint

        return fingerprint(
            {
                "case_id": self.case_id,
                "version": self.version,
                "instructions": self.instructions,
                "fixture": None if self.fixture is None else self.fixture.digest,
                "tool_config": dict(self.tool_config),
                "verifier": self.verifier,
                "verifier_digest": self.verifier_digest,
                "max_turns": self.max_turns,
                "max_runtime_seconds": self.max_runtime_seconds,
                "allowed_faults": list(self.allowed_faults),
                "model": self.model,
                "repeats": self.repeats,
                "seed": self.seed,
            }
        )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "case_id": self.case_id,
            "version": self.version,
            "digest": self.digest,
            "instructions": self.instructions,
            "instructions_ref": self.instructions_ref,
            "fixture": None if self.fixture is None else self.fixture.to_record(),
            "tool_config": dict(self.tool_config),
            "verifier": self.verifier,
            "verifier_digest": self.verifier_digest,
            "max_turns": self.max_turns,
            "max_runtime_seconds": self.max_runtime_seconds,
            "allowed_faults": list(self.allowed_faults),
            "model": self.model,
            "repeats": self.repeats,
            "seed": self.seed,
            "expectations": dict(self.expectations),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "EvalCase":
        fixture = record.get("fixture")
        return cls(
            case_id=str(record.get("case_id") or ""),
            version=str(record.get("version") or ""),
            instructions=str(record.get("instructions") or ""),
            instructions_ref=str(record.get("instructions_ref") or ""),
            fixture=None if fixture is None else FixtureSpec.from_record(fixture),
            tool_config=dict(record.get("tool_config") or {}),
            verifier=str(record.get("verifier") or ""),
            verifier_digest=str(record.get("verifier_digest") or ""),
            max_turns=int(record.get("max_turns") or 1),
            max_runtime_seconds=record.get("max_runtime_seconds"),
            allowed_faults=tuple(str(item) for item in record.get("allowed_faults") or ()),
            model=str(record.get("model") or "offline"),
            repeats=int(record.get("repeats") or 1),
            seed=record.get("seed"),
            expectations=dict(record.get("expectations") or {}),
        )


@dataclass(frozen=True)
class VerifierOutcome:
    """The independent verifier's answer, with the evidence it decided from."""

    passed: bool
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)
    #: True when the verifier itself could not run (a missing tool, a broken fixture): that is
    #: an infrastructure error, not a task failure.
    infra_error: str = ""

    def __post_init__(self) -> None:
        if self.infra_error and self.passed:
            raise ContractError("a verifier that could not run cannot also pass the case")
        if not self.passed and not self.reasons and not self.infra_error:
            raise ContractError("a failed verification must say why")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
            "infra_error": self.infra_error,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "VerifierOutcome":
        return cls(
            passed=bool(record.get("passed")),
            reasons=tuple(str(item) for item in record.get("reasons") or ()),
            evidence=dict(record.get("evidence") or {}),
            infra_error=str(record.get("infra_error") or ""),
        )


@dataclass(frozen=True)
class EvalCaseResult:
    """One case run: the protocol facts, the verdict, and what is not known.

    ``protocol`` holds the recovery/commit facts the offline line measures (which attempt ran
    what, whether a confirmed call was re-run, whether the budget stopped a dispatch).
    ``usage`` holds actual/estimated/unknown measurements with their sources, never a single
    number that hides what is missing.
    """

    case_id: str
    case_version: str
    case_digest: str
    attempt: str
    outcome: EvalOutcome
    run_id: str = ""
    verifier: VerifierOutcome | None = None
    protocol: Mapping[str, Any] = field(default_factory=dict)
    stop_code: str = ""
    error_code: str = ""
    final_text: str = ""
    elapsed_seconds: float | None = None
    usage: tuple[Mapping[str, Any], ...] = ()
    human_decisions: int = 0
    artifacts: Mapping[str, str] = field(default_factory=dict)
    report: Mapping[str, Any] | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    repeat_index: int = 0
    schema_version: int = EVAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ContractError("an eval result needs the case it answers")
        if self.outcome is EvalOutcome.PASSED and self.verifier is not None and not self.verifier.passed:
            raise ContractError(
                f"{self.case_id}: a case cannot pass while its verifier says it failed"
            )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "case_version": self.case_version,
            "case_digest": self.case_digest,
            "attempt": self.attempt,
            "repeat_index": self.repeat_index,
            "outcome": self.outcome.value,
            "run_id": self.run_id,
            "verifier": None if self.verifier is None else self.verifier.to_record(),
            "protocol": dict(self.protocol),
            "stop_code": self.stop_code,
            "error_code": self.error_code,
            "final_text": self.final_text,
            "elapsed_seconds": self.elapsed_seconds,
            "usage": [dict(item) for item in self.usage],
            "human_decisions": self.human_decisions,
            "artifacts": dict(self.artifacts),
            "report": None if self.report is None else dict(self.report),
            "diagnostics": dict(self.diagnostics),
            "notes": list(self.notes),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "EvalCaseResult":
        version = record.get("schema_version")
        if version not in SUPPORTED_EVAL_SCHEMA_VERSIONS:
            raise VersionRefused("eval result", version, SUPPORTED_EVAL_SCHEMA_VERSIONS)
        verifier = record.get("verifier")
        return cls(
            case_id=str(record.get("case_id") or ""),
            case_version=str(record.get("case_version") or ""),
            case_digest=str(record.get("case_digest") or ""),
            attempt=str(record.get("attempt") or ""),
            outcome=EvalOutcome(str(record.get("outcome") or EvalOutcome.NOT_RUN.value)),
            run_id=str(record.get("run_id") or ""),
            verifier=None if verifier is None else VerifierOutcome.from_record(verifier),
            protocol=dict(record.get("protocol") or {}),
            stop_code=str(record.get("stop_code") or ""),
            error_code=str(record.get("error_code") or ""),
            final_text=str(record.get("final_text") or ""),
            elapsed_seconds=record.get("elapsed_seconds"),
            usage=tuple(dict(item) for item in record.get("usage") or ()),
            human_decisions=int(record.get("human_decisions") or 0),
            artifacts=dict(record.get("artifacts") or {}),
            report=record.get("report"),
            diagnostics=dict(record.get("diagnostics") or {}),
            notes=tuple(str(item) for item in record.get("notes") or ()),
            repeat_index=int(record.get("repeat_index") or 0),
            schema_version=int(version),
        )

    def replace(self, **changes: Any) -> "EvalCaseResult":
        return replace(self, **changes)


@dataclass(frozen=True)
class EvalSuite:
    """A frozen set of cases, with the digest that makes two suites comparable."""

    name: str
    version: str
    cases: tuple[EvalCase, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not self.version:
            raise ContractError("an eval suite needs a name and a version")
        seen: set[str] = set()
        for case in self.cases:
            if case.case_id in seen:
                raise ContractError(f"suite {self.name}: duplicate case id {case.case_id!r}")
            seen.add(case.case_id)

    @property
    def digest(self) -> str:
        from ..versioning import fingerprint

        return fingerprint(
            {
                "suite": self.name,
                "version": self.version,
                "cases": [case.digest for case in self.cases],
            }
        )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": EVAL_SCHEMA_VERSION,
            "name": self.name,
            "version": self.version,
            "digest": self.digest,
            "cases": [case.to_record() for case in self.cases],
        }


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return tuple(out)
