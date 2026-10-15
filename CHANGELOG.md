# Changelog

All notable changes to this package are recorded here. This project is an alpha:
breaking changes are allowed between versions, and **persistent formats still
require a migration or an explicit refusal** — an alpha is not a licence to drop
a user's existing session (see `docs/contract.md` and `docs/recovery.md`).

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
