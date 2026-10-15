"""Neutral error taxonomy for the harness.

Every error raised by this package derives from :class:`HarnessError` and
carries a machine-readable :attr:`HarnessError.code`. Application code may map
these onto its own exceptions, but these types must never inherit from an
application's exceptions: a host that catches its own story/document errors must
not accidentally swallow harness faults, and vice versa.

The taxonomy deliberately separates the states that must never be conflated:

``ProviderFailure``
    The model provider failed, timed out or returned invalid output.
``VersionRefused``
    Persisted state, an event or a checkpoint carries a version this build
    cannot prove safe. Nothing is guessed and nothing is upgraded.
``OutcomeUnknown``
    An external side effect may or may not have happened and cannot be proven.
    It is never auto-retried and never reported as success.

Tool-call *results* are a separate vocabulary, because a refused tool call is an
outcome the model must see rather than an error the process raises:
:mod:`yanxu_harness.tools.outcomes` (``ToolRejected``, ``ToolArgsInvalid``,
``ToolDeferred``, ``ToolCancelled``). This module deliberately does **not** define
a second set of ``Tool*`` classes — two vocabularies for the same concept is how
``except`` clauses start silently missing things.
"""

from __future__ import annotations

__all__ = [
    "HarnessError",
    "ConfigurationError",
    "ContractError",
    "VersionRefused",
    "BudgetExceeded",
    "ProviderFailure",
    "OutcomeUnknown",
    "RunBusy",
    "RunNotFound",
    "RunRequestDrift",
]


#: ``StorageConflict`` lives in :mod:`yanxu_harness.storage.ports` and the skill
#: errors in :mod:`yanxu_harness.skills.provider`, because they belong to the port
#: that raises them. They are all :class:`HarnessError` subclasses.


class HarnessError(Exception):
    """Base class for every error raised by this package."""

    code = "harness_error"


class ConfigurationError(HarnessError):
    """The host configured the harness in a way it cannot honour."""

    code = "configuration_error"


class ContractError(HarnessError):
    """A registration, definition or payload broke a stated contract."""

    code = "contract_error"


class VersionRefused(HarnessError):
    """A versioned artifact is unknown, future, or not provably safe."""

    code = "version_refused"

    def __init__(self, what: str, version: object, supported: object) -> None:
        super().__init__(
            f"{what} version {version!r} is not supported by this build "
            f"(supported: {supported!r}); refusing instead of guessing"
        )
        self.what = what
        self.version = version
        self.supported = supported


class BudgetExceeded(HarnessError):
    """A declared budget was exceeded; the request was not dispatched."""

    code = "budget_exceeded"

    def __init__(self, what: str, detail: str = "") -> None:
        super().__init__(f"{what} budget exceeded" + (f": {detail}" if detail else ""))
        self.what = what
        self.detail = detail


class ProviderFailure(HarnessError):
    """The provider failed, timed out, or produced invalid output."""

    code = "provider_failure"

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"provider failure ({kind})" + (f": {detail}" if detail else ""))
        self.kind = kind
        self.detail = detail


class OutcomeUnknown(HarnessError):
    """An external side effect cannot be proven either way."""

    code = "outcome_unknown"

    def __init__(self, operation_id: str, detail: str = "") -> None:
        super().__init__(
            f"operation {operation_id} has an unknown outcome"
            + (f": {detail}" if detail else "")
            + "; it will not be retried automatically"
        )
        self.operation_id = operation_id
        self.detail = detail


class RunNotFound(HarnessError):
    """No run with that identity was ever persisted (or it was pruned)."""

    code = "run_not_found"

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} is not in the store")
        self.run_id = run_id


class RunBusy(HarnessError):
    """A second invocation arrived for a run that is already active.

    Two loops for one run would double its model requests and could settle the
    same tool batch twice, so a duplicate submission is refused instead of
    started. The refusal carries no state change: the active invocation keeps
    owning the run.
    """

    code = "run_busy"

    def __init__(self, run_id: str, detail: str = "") -> None:
        super().__init__(
            f"run {run_id!r} is already running" + (f": {detail}" if detail else "")
        )
        self.run_id = run_id
        self.detail = detail


class RunRequestDrift(HarnessError):
    """A second invocation carried different input for the same run identity.

    The stored run is *not* overwritten: an approval, a checkpoint or a finished
    outcome that was produced for other input must not be replaced by whatever
    arrived later.
    """

    code = "run_request_drift"

    def __init__(self, run_id: str, detail: str = "") -> None:
        super().__init__(
            f"run {run_id!r} was already submitted with different input"
            + (f": {detail}" if detail else "")
            + "; the stored run was left untouched"
        )
        self.run_id = run_id
        self.detail = detail
