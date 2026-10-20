# Changelog

All notable changes to this package are recorded here. This project is an alpha:
breaking changes are allowed between versions, and **persistent formats still
require a migration or an explicit refusal** — an alpha is not a licence to drop
a user's existing session (see `docs/contract.md` and `docs/recovery.md`).

## 0.3.0a1 — MCP client adapter, public alpha distribution (2026-10-20)

The MCP implementation and all five independent-review findings have passed local
review. This alpha adds the optional client adapter and the public tool-result and
dispatch-guard hooks below. The alpha is packaged for distribution through PyPI.
See [the original local release record](docs/release-0.3.0a1.zh-CN.md) for artifact verification and
the host adoption results. Historical implementation gates below used `0.2.0a1`.

* **A neutral tool result.** `ToolExecutionResult` lets a handler declare
  `SUCCEEDED`/`FAILED`/`UNKNOWN` explicitly. Plain return values keep their previous
  meaning (a success), both ledgers normalise through one function, a declared success
  that cannot be stored becomes `UNKNOWN` for a mutating tool and `FAILED` for a
  read-only one, and the handler now sees the gateway's authoritative `operation_id`
  through `RunContext.for_operation` (a host-owned context is passed through unchanged
  and a host-owned ledger still records exactly one intent and one receipt).
* **An optional MCP client adapter** (`pip install 'yanxu-harness[mcp]'`): Tools over
  stdio and Streamable HTTP for protocol revisions `2025-11-25` and `2026-07-28`,
  pinned to `mcp==2.3.0`. The SDK is imported lazily, so a base install imports every
  submodule without pulling it in. Included are bounded stdio and HTTP transports
  (per-frame/per-body byte limits, bounded stderr, bounded process-group shutdown),
  a connection manager with per-scope isolation and deadlines that cover slot
  waiting, bounded discovery with stable local aliases, real JSON Schema validation,
  frozen definition versions with drift refusals, a result codec, receipt facts, and
  read-only diagnostic projections.
* **One admission sends at most one `tools/call`.** The adapter uses the public
  low-level `ClientSession`; the high-level `Client.call_tool` resends the same call
  after a `HEADER_MISMATCH` rejection, which was reproduced against a real loopback
  server before choosing the entry point.
* **Unknown stays unknown.** A request that may have reached the server is recorded as
  `UNKNOWN` and is never replayed automatically; business evidence is required to
  promote it, and a settled receipt is reused instead of re-run.
* Evidence, limits, refusals and open items are recorded in
  `docs/mcp-client-sdk-baseline.zh-CN.md`, `docs/mcp-client-progress.zh-CN.md` and
  `docs/mcp-client-delivery-report.zh-CN.md`.
* **Independent-review fixes.** The five blocking gaps found by the independent review
  (`docs/mcp-client-independent-review.zh-CN.md`) are closed and covered by default tests:
  every schema-retrieval keyword (`$ref`/`$dynamicRef`/`$recursiveRef`) is refused and the
  compiled validator is bound to a registry that never retrieves; a catalog refresh observed
  by the client invalidates the frozen adapter (and a host tool can refuse a resume before
  dispatch via the new optional `dispatch_guard`); a *failed* refresh (a directory read error,
  a missing bound tool or an unusable schema) also invalidates that connection scope's old
  catalog until a new snapshot is published successfully; remote text is scrubbed of the
  host's own credentials before it becomes a receipt or model content; and a
  bound-but-missing tool now fails the binding assembly instead of yielding a `complete`
  empty catalog. MCP-specific fault windows (intent, receipt, promotion and partial-batch
  crashes) run in real child processes in `tests/mcp/test_fault_windows.py`.

## 0.2.0a1 — local alpha distribution, not published to a package index

This milestone consolidates context engineering, per-tool recovery, diagnostics and
durable task orchestration on the single Core Runner.

* Durable inbox admission and application share the Run's control-store transaction.
  Pending inputs are selected by state and kind, so older follow-ups remain visible
  after newer steering inputs and across long histories.
* A bounded local worker schedules tasks with fenced claims, namespace limits,
  absolute task deadlines and shared budget reservations. Recoverable parent/child
  work uses stable spawn semantics, completion notifications and durable result waits.
* Parent recovery rechecks deployment fingerprints and committed child evidence,
  preserves the original turn budget, applies each child result once, and resumes
  committed preparation states after an executor exits. Budget exhaustion remains a
  terminal stop and records the owned children's cancellation/settlement policy.
* Committed-state recovery now applies the task's remaining absolute deadline before
  executing the Runner, including the case where the deadline expired during downtime.
* Context projection, controlled summaries and output references preserve the full
  transcript; tool batches persist and recover each call using the operation ledger.
* Read-only run/task diagnostics and offline evaluations expose execution evidence.
  Evaluation identities include acceptance conditions and command policy; command
  output is bounded during collection and sample matching does not duplicate costs.

Package versions and persisted-format versions are independent. See the
[public contract](docs/contract.md), [recovery rules](docs/recovery.md) and
[verification record](docs/verification.md) for supported records, conservative
legacy refusals and the actual test/platform boundaries. Python 3.10+ remains the
declared floor and the base install remains standard-library only.

## 0.1.0a2 — local candidate, not published

The alpha that makes the end-to-end chain runnable and recoverable. `0.1.0a1` had
the parts; `0.1.0a2` connects them and writes down how they behave.

What is new:

* **A concrete harness** (`yanxu_harness.runtime.DefaultHarness`) implementing
  `start`/`resume`/`cancel`/`state` with constructor-injected dependencies. Run state
  is read from the store, so a new instance sees the same run; a duplicate
  submission reuses it, different input is refused (`RunRequestDrift`), and an active
  run raises `RunBusy` instead of starting a second loop.
* **Explicit results.** `LoopResult` and `RunStatus` (now including `RUNNING`) carry
  a machine-readable reason for every exit: a budget stop is `USAGE_LIMITED`, a model
  failure `FAILED` with the provider's own kind, a host stop hook `COMPLETED` with
  `stop_code=stop_hook`. Nothing is inferred from the answer's text, the last
  message's role or the number of events.
* **A pause is an outcome, not a list.** The list-returning entries
  (`run_agent_loop`, `run_agent_loop_continue`) raise `LoopSuspended` — carrying the
  readable `LoopResult` — instead of returning messages that look like a finished
  answer. `Agent` gained `prompt_result` / `continue_run_result` / `last_result`
  while `prompt()` keeps its signature and raises the same control signal. A model
  turn that ends with `stop_reason=deferred` suspends too (its tool calls are not
  dispatched), and a failing host hook is `LoopStop.INTERNAL_ERROR`, kept apart from
  a provider failure so retries cannot cross over.
* **One runner, explicit state.** The two-layer loop became a pure reducer
  (`core/transitions.py#advance`, state and actions in `core/execution.py`) driven by
  one runner (`core/runner.py#Runner`). Hooks, the model request and tool batches are
  actions the reducer asks for and the runner performs; a duplicate or late event is
  ignored with a reason, a conflicting one is refused. Every entry point — the list
  and structured functions, `Agent`, `DefaultHarness` — goes through that runner, and
  `core/agent_loop.py` is now a re-export of the same objects. Tool concurrency is
  configured explicitly (`max_concurrent_tools`) without changing event, message or
  result ordering.
* **An explicit state at the durable boundary.** The reducer's state is committed through
  a port (`AgentLoopConfig.state_sink`) called before any action runs, so a crash inside a
  tool batch leaves a durable record of the model's response and the calls it declared —
  with no outcome claimed for them. `RunRecord.execution` stores that snapshot (optional:
  a record without one is read from `messages`/`batch` alone), `RunOutcome.pending_work`
  and `RunState.pending_work` project it (`blocking`, `replayable`, `declared`,
  `batch_outcome_unknown`, `model_request_outstanding`), a resume refuses to continue when
  the snapshot and the recorded batch disagree about which calls are waiting, and an
  orphaned run names the calls it had declared instead of only saying "unrecorded".
* **Cancellation and isolation.** A result that answers a call this turn never declared
  (a stale callback, or two answers to one call) is refused instead of applied; a late
  event after a run ended cannot start a new turn; a cancellation the run observed while
  paused is durable and a resume turns that pause into the `CANCELLED` it already is
  without dispatching anything (`EventKind.RUN_CANCELLED`); and two agents on one loop
  do not block each other while a listener is slow.
* **Suspension and resume.** A batch that needs a decision (or contains an unproven
  operation) stops the run before the next model request and commits the run state,
  the recorded batch and the events in one transaction. A resume reuses recorded
  results, re-dispatches waiting calls through the same gateway under the same
  operation id, settles an unproven operation only from host-supplied business
  evidence, and refuses a drifted arrangement with the drift named.
* **A provider→core adapter** and a **tool→core adapter**, so a real streaming
  provider drives the one loop and every tool call goes through the gateway. The
  blocking HTTP transport is pumped on a worker thread: a slow model no longer
  freezes the event loop.
* **Event envelopes and fences.** Every published event carries namespace, run,
  attempt, order and schema, and an event from another attempt is refused before
  anything is mutated.
* **Leases and identity isolation.** Commits carry the run's lease, the fencing token
  is checked inside the write, and derived operation ids are composite
  (`namespace:session:run:call`) so two projects cannot collide.
* **Cancellation** reaches the provider, the tools and the drive task, and a run
  cancelled from another thread stops within a bound.
* **An end-to-end example** (`examples/05_durable_approved_edit.py`) that runs the
  whole chain in child processes, plus an optional real-provider variant outside the
  gate.
* `docs/verification.md` records what is verified by which test, and what is not.

Behaviour changes worth noting when upgrading from `0.1.0a1`:

| Change | Consequence |
| --- | --- |
| `RunStatus` moved to `yanxu_harness.core.outcome` and gained `RUNNING` | `yanxu_harness.runtime.RunStatus` is the same object; code matching on the enum keeps working, but a terminal-status set that assumed the old five members should use `TERMINAL_STATUSES` |
| `RunRecord` is written under a schema version | a record without one is refused rather than reconstructed |
| `EventEnvelope` is what a run listener receives | a listener typed for `Event` should take the envelope (`Event` remains the store-level type) |
| derived operation ids are composite | an explicit `operation_id` is unchanged; a derived one now includes the namespace, session and run |
| the `Persistence` port requires `commit_transition` | a host store must implement it (state and events in one transaction) |

## 0.1.0a1 — local candidate, not published

First working version. There is no release date for a published artifact because
nothing has been published: the source repository exists, but there is no package
index, no release account behind an upload and no tag. The distribution name
`yanxu-harness`, the import name `yanxu_harness` and the version string are this
package's own choices, not verified names on an index.

What is in it:

* **One model loop** (`yanxu_harness.core`): neutral messages, streaming events,
  tool batches, turn budgets, steering/follow-up queues, cancellation. No second
  implementation hides behind a shim — compatibility paths re-export the same
  objects.
* **Run lifecycle** (`yanxu_harness.runtime`): run identity, principal,
  cancellation tokens and a suspend/resume contract with versioned state.
* **Tools in four layers** (`yanxu_harness.tools`): definition+registry,
  deployment visibility, per-turn declaration/discovery, and
  authorization+execution through a single gateway. Discovery takes effect on the
  *next* batch and grants nothing; rejected calls leave no intent behind.
* **Skills in three layers** (`yanxu_harness.skills`): metadata catalog,
  on-demand body/resources, session-bound version fingerprints. Skill content is
  untrusted guidance and cannot authorize or execute anything.
* **Optional decision routing** (`yanxu_harness.decisions`): no-op, rule and
  model-backed routers behind one port, with bounded payloads, a call budget, a
  cache and per-kind validation. A host that configures no decision model still
  runs every task.
* **Context management** (`yanxu_harness.context`): the raw transcript archive
  and the model projection are separate; summarization is a budgeted view and
  never rewrites the archived history; a final payload gate measures the exact
  serialized bytes before dispatch.
* **Persistence and recovery** (`yanxu_harness.storage`): store and
  unit-of-work ports with in-memory and SQLite reference implementations,
  operation intent/receipts, approval binding, lease/CAS fencing and
  cross-process fault windows. External side effects are reported as `unknown`
  when they cannot be proven, and are never retried blindly.
* **Providers** (`yanxu_harness.providers`): a provider protocol plus explicitly
  configured transports. Credentials come from the host; the package ships no
  network client of its own.
* **Examples**: basic dialogue, an approved data edit, cross-process recovery,
  and a non-fiction pipeline combining custom tools, a skill and an optional
  decision router.

Not in this version (deliberate): automatic skill marketplaces, remote plugin
installation, distributed scheduling, multi-agent swarms, automatic provider
switching and production sandboxing. Extension points are kept, but no generic
platform is built ahead of need.

## Format changes

| Version | Change |
| --- | --- |
| 0.1.0a1 | initial event/checkpoint/schema versions; unknown or future versions are refused rather than guessed |
| 0.1.0a2 | run records gain a schema version (`SUPPORTED_RUN_RECORD_VERSIONS`); event envelopes gain their own (`SUPPORTED_EVENT_VERSIONS`); checkpoints gain the third partition class `uncertain_tool_call_ids`, which an older record reads as empty — the only backwards-compatible reading. A future or missing version is refused. |
