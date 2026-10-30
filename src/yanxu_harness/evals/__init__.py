"""Task evaluation: frozen cases, an independent verifier, and comparable results.

Two kinds of evaluation share this package, and mixing them up is the failure mode it
exists to prevent:

* **offline deterministic evaluation** — a scripted model, the real harness, the real
  SQLite stores, the real Gateway, and a fixed fault injection. It measures *protocol*
  facts (does a recovery reuse what was confirmed? does a budget stop dispatching? is the
  side effect produced exactly once?) and it is the default, used by CI;
* **real model task evaluation** — an explicitly configured adapter, model, repeat count
  and budget. It measures how well a model does a task. It is never the default, and a
  scripted result is never reported as evidence about a model's ability.

What a case must state, and why each field is not optional: the task's own version and
fingerprint (so two runs are comparable), the fixture's version and content digest (so a
"result" belongs to the fixture that produced it), the verifier (so the pass/fail decision
is not made by the thing being tested), and the budgets and allowed fault injections (so a
run cannot quietly change the question).

An **independent verifier** is the point of the whole exercise. A model saying "done" is
result *content*; it passes nothing. The verifier reads the workspace, the run store and the
operation ledger, runs the fixture's own tests in a subprocess, and decides. Its truth and
its code live outside the workspace the run may write to.

One limit stated up front, because it would be easy to read this as more than it is: **this is
not a sandbox.** A case gets an isolated directory and its commands run with an explicit argv,
cwd, timeout and bounded output, but a tool that decided to reach outside its workspace could.
Containment here is a policy for honest tools, not an operating-system boundary; real isolation
is the host's to provide.

"Bounded output" means bounded *while it is read* (`evals/commands.py`), not trimmed after the
fact: each stream is read in fixed-size chunks, at most the policy's byte allowance is kept, the
dropped bytes are counted, and the result says so. A command whose stream cannot be read to the
end — a grandchild holding the pipe open — is reported with `readers_finished: false` and the
rest of its output is unknown rather than assumed empty.
"""

from __future__ import annotations

from .cases import CASES, case_by_id, case_set_digest
from .commands import (
    DEFAULT_COMMAND_POLICY,
    CommandPolicy,
    CommandResult,
    descendant_cleanup_capability,
    run_command,
)
from .compare import compare_reports
from .runner import EvalSuiteResult, run_case, run_suite
from .types import (
    EVAL_SCHEMA_VERSION,
    EvalCase,
    EvalCaseResult,
    EvalOutcome,
    EvalSuite,
    FixtureSpec,
    VerifierOutcome,
)
from .timeline_cases import (
    TIMELINE_CASES,
    TIMELINE_CASES_VERSION,
    TIMELINE_SUITE,
    run_timeline_case,
    run_timeline_suite,
    timeline_case_by_id,
)

__all__ = [
    "CASES",
    "case_by_id",
    "case_set_digest",
    "CommandPolicy",
    "CommandResult",
    "DEFAULT_COMMAND_POLICY",
    "compare_reports",
    "descendant_cleanup_capability",
    "run_command",
    "EVAL_SCHEMA_VERSION",
    "EvalCase",
    "EvalCaseResult",
    "TIMELINE_CASES",
    "TIMELINE_CASES_VERSION",
    "TIMELINE_SUITE",
    "timeline_case_by_id",
    "run_timeline_case",
    "run_timeline_suite",
    "EvalOutcome",
    "EvalSuite",
    "EvalSuiteResult",
    "FixtureSpec",
    "run_case",
    "run_suite",
    "VerifierOutcome",
]
