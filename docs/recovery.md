# Failure and recovery boundaries

What this package guarantees after a crash, what it deliberately refuses to
guess, and how to tell the difference. Every claim here is exercised by a test
that kills a real process (`tests/fault_windows.py`) or by
the durable store's own adversarial cases.

## The states a call can end in

| State | Means | Retry? |
| --- | --- | --- |
| `executed` | the handler ran *in this call* and the receipt is `succeeded` | no |
| `replayed` | a settled receipt already existed; the handler did **not** run again | no |
| `awaiting_approval` | waiting for a human decision; nothing ran, no ledger entry | after a decision |
| `denied` | policy or the author refused it | no (re-deciding the same call stays denied) |
| `invalid_args` | validation failed; nothing ran, no ledger entry | with fixed arguments |
| `cancelled` | the run was cancelled before the handler | no |
| `unknown` | the handler may have run and the outcome cannot be proven | **never automatically** |

`unknown` is the important one. It exists because an external side effect and a
local ledger cannot share a transaction. The package's answers are:

* the ledger records the intent **before** the handler runs;
* `EffectState.UNKNOWN` is a real state, not an error;
* `should_retry(UNKNOWN)` is always `False`, even for a tool declared idempotent;
* an operation that was never settled is exposed as `UnsettledOperation`, whose
  `provable` property is `False` — the store does not decide on its own that it
  failed.

To resolve one you need business evidence: ask the external system whether the
write landed (`external_id` is there for exactly this), or make the tool
idempotent with an `Idempotency.KEYED` key so a repeated call is a no-op.

## The four failure windows

Kill a process at each arrow and see what a restart can know:

```
begin(intent) ──▶ handler runs ──▶ settle(receipt)
     │                 │                │
     │                 │                └─ (4) died after the receipt: the next
     │                 │                     process REPLAYS it; the handler runs once
     │                 └─ (3) died here: the effect may have landed.
     │                        State: unknown, needs reconciliation.
     └─ (2) died here: nothing ran. The ledger has an open operation.
```

Plus the state-transition window: `commit_transition` writes state **and** events
in one transaction, and the fanout happens after the commit. If the process dies
between the two, a restart replays the events from a cursor. The business fact is
never rolled back to "fix" a lost notification.

## Leases and late workers

* a lease carries a monotonic fencing token **per resource**; the token is never
  reused, not even after the lease is released;
* a save names the lease it believes it holds, and the check happens inside the
  same transaction as the write — a worker that lost its lease cannot land a late
  write at all;
* a lease held by a process that died is still held until it expires. That is
  deliberate: treating a crashed holder's lease as free is how two writers appear.

## Approvals across a restart

An approval is a fact about a decision. It survives a restart (with
`SqliteApprovalStore`), and it stops applying when anything it was bound to
changes:

| Drift | What happens |
| --- | --- |
| arguments changed | the approval no longer applies; the author is asked again with a *new* binding |
| target version changed | same |
| tool contract version changed | same |
| preview changed | same |
| policy version changed | same |
| expired | same |
| the author denied it | the call stays denied; the same question is not re-asked |

The earlier decision is kept as history rather than overwritten, so an audit can
see "the author approved a different version earlier".

## Suspending and resuming a run

A run that meets a call waiting for a decision (or an operation whose outcome could
not be proven) **stops before the next model request**. At that moment the harness
commits, in one transaction, the run state, the recorded batch and the durable
events. A restarted process therefore reads a pause rather than reconstructing one.

Resuming is where a second write would happen if the rules were loose, so:

* a call with a receipt has its recorded result **reused**, not re-dispatched;
* a waiting call is re-dispatched through the same gateway with the same operation
  id. An approved binding executes the call once; every later attempt replays that
  receipt. A gateway that reports a *different* operation id is refused, because
  dispatching under a new id could duplicate the effect;
* an unproven operation stays `unknown` until a host-supplied reconciler returns
  business evidence (`proven=True` and the observed state). Without it the run stays
  suspended and the operation is never retried;
* a resume whose config, tool set, instructions, policy or visible-tool manifest
  drifted is **refused** with the drift named: the approval was granted for the old
  arrangement. An *expired* approval is the exception — it goes back to the author,
  and the gateway supersedes the binding.

Three ledgers are involved (run state, operation receipts, approvals). They may live
in three files or three databases, and the package does **not** claim they share a
transaction. Recovery works from references and receipts instead: the batch record
says which operation each call used, and the operation ledger answers what actually
happened. That is also why `unknown` exists at all.

```text
pause:    state + batch + events  ── one transaction
resume:   read batch → reuse settled receipts → re-dispatch only waiting calls
                        → reconcile unproven ones (evidence only)
          → one result per original tool_call_id → continue the model loop
```

A run may pause more than once, each time under a new attempt. Nothing about a
resume may reuse the *previous* attempt's identity: namespace, session and run stay
the same, and the attempt advances so a late callback from the old attempt cannot be
mistaken for a current one.

## What the committed execution state adds

A paused or interrupted run commits more than its transcript: `RunRecord.execution`
holds the explicit execution state (`core.execution.state_to_record`), and
`RunOutcome.pending_work` / `RunState.pending_work` project it for the host.

| Field | Question it answers | How to act on it |
| --- | --- | --- |
| `blocking` | which calls must not be dispatched again | a decision (`awaiting`) or business evidence (`uncertain`) is missing; nothing runs until it arrives |
| `replayable` | which calls provably never ran | only calls recorded `unstarted` *outside* a dispatch window; inside one, use `unobserved` and the ledger instead |
| `unobserved` | which calls this driver reported no observation for | query the operation ledger for these call ids; a receipt settles the call, an open intent without a receipt is `unknown`, and only a call with no intent at all may be re-dispatched |
| `calls` | the per-call partition | one entry per declared call with its source order, `outcome`, schedule `progress`, ledger reference and whether a result is recorded |
| `declared` + `batch_outcome_unknown` | the process died inside a dispatch window | query the operation ledger for these call ids; a receipt settles the call, an open intent without a receipt is `unknown`, and only a call with no intent at all may be re-dispatched |
| `model_request_outstanding` | the process died inside a model request | not a tool side effect — no external effect can follow from an unfinished request, so the next attempt may ask again |
| `interaction` | what the run is waiting for | the approval or reconciliation question to answer first |

Two properties hold at every boundary:

* the state is committed **before** the actions of that transition run, so the model's
  final response is durable before its tool calls are dispatched (a crash inside a
  batch therefore leaves the declared calls on disk with no outcome claimed for them);
* a resume whose committed snapshot and recorded batch disagree about which calls are
  waiting is **refused**, because choosing either would be a guess about what ran.

### Per-call settlement and per-call recovery

A batch is a set of calls, and each of them is its own fact. The driver therefore reports
one *observation* per call (`ToolCallObserved`), the reducer records exactly that call, and
the host's commit port confirms it — so a call that finished while its siblings were still
running is durable on its own, not only once the batch is over. A sequential batch waits
for the driver to acknowledge each observation before dispatching the next call, which is
what makes "the prefix is settled before the next call starts" a property of the code.

Three consequences a reader should know:

* **an observation is not a success.** `awaiting`, `unknown`, `denied`, `failed` and the
  explicit "not executed because the run was cancelled" answer travel through the same
  input. The name says what the driver now knows, not what happened;
* **the ledger is the only proof of absence.** `unstarted` inside a dispatch window means
  "this build has no observation", never "this call did not run". Only the operation
  ledger may say that (see `ToolGateway.review` and the `absent / open / receipt /
  unavailable` answers in `tools/execution.py`), and a ledger that cannot answer *blocks*;
* **one uncertain call stops the batch.** Recovery classifies every call before it
  dispatches anything: when something is unproven, the confirmed facts are committed and
  the run pauses, so a call the ledger proved absent does not run ahead of a question that
  is still open.

Recovery of a batch whose process died reads the committed declaration partition and
continues it through the *same* Runner (`Runner.resume_batch`), keyed by the recorded model
action. It does not re-ask the model and it does not rebuild the state from the transcript,
which would lose the original action and the per-call progress.

An execution snapshot is private recovery state: it keeps the provider continuation
material a message holds, and it is never published as an event payload. The public
projection strips the continuation fields recursively, and the event boundary refuses
those names outright — `docs/verification.md` (T20, T22) records which tests prove it.

## Cancellation while a run is paused

`cancel()` on a run this process does not drive only **records** the request: the run
may still be alive elsewhere, so it is not marked cancelled. If the run was waiting, a
later `resume` observes that recorded request and commits the pause as the `CANCELLED`
it already is (`stop_code=aborted`), dispatching nothing and keeping the recorded batch
as the evidence of what was waiting. The first recorded reason wins.

## Duplicate submissions

Two calls for the same run must not produce two loops:

| The second call is | What happens | Why |
| --- | --- | --- |
| the *same* request (a retry) | the stored state is returned; no second model request | the request fingerprint matches, so it is the same run |
| a *different* request | `RunRequestDrift`; the stored run is untouched | an approval or a finished outcome produced for other input must not be replaced |
| for a run that is active in this process | `RunBusy` | two loops for one run would double its requests and could settle one batch twice |
| for a run that is active in *another* process | `RunBusy` while its lease is valid (see above) | the lease is the cross-process half of the same rule |

The lease is claimed by `start`/`resume` and released when the invocation ends — after
its terminal commit, never before (releasing first would make the run's own final write
a stale write). An orphaned run (a crash before the pause was recorded) can only be
turned into a reconciliation wait once the lease is free, which is what proves its
previous driver is gone.

## Cross-store consistency

The three ledgers may be three files (the example uses exactly that):

```text
runs.db        the run record: status, attempt, batch, interactions, transcript
operations.db  the operation ledger: intent → receipt, with the effect state
approvals.db   the approval decisions, kept as history
```

They are **not** claimed to share a transaction. Recovery therefore works from
references rather than from a distributed commit:

* the run record names each call's `operation_id`, so a resume can ask the ledger
  what actually happened to it;
* the operation ledger answers with a receipt or with "there is an intent and no
  receipt" — and that second answer means *unprovable*, not *did not happen*;
* the approval ledger answers what a human decided, and the gateway checks the
  binding (arguments, target version, tool version, preview, policy version, expiry)
  before honouring it, superseding it when it no longer applies;
* a missing reference, a status this build cannot classify, or a checkpoint whose
  partition does not cover its declared calls is a refusal, never a reconstruction.

The practical consequence: a resume can be blocked by evidence it cannot obtain. It
then stays `suspended` with a reconciliation interaction — which is the honest outcome,
and better than a guess in either direction.

## Budgets and refusals

* **Context**: capacity is window − output reserve − margin. An unknown window is
  reported as unknown; no percentage is invented, and no fixed character cutoff
  is reapplied.
* **Payload**: the gate measures the *final serialized bytes* immediately before
  dispatch. A refused payload sends nothing — no partial request, no retry.
* **Decisions**: the call budget, the time budget and the input/output sizes are
  all bounded; exceeding any of them degrades the decision with a reason. A
  decision is never a precondition for the task itself.
* **Skills**: metadata, body, resource size and resource count are bounded; an
  over-budget skill fails at load with a named limit.

## What the package does not promise

* **Not exactly-once.** No toolkit can promise that for arbitrary external
  effects. The package makes the ambiguity *visible* instead of hiding it.
* **Not cross-database atomicity.** Local state and a remote system are two
  systems. The receipt and the idempotency key are the bridge.
* **Not "the model will eventually succeed".** A provider failure is a state,
  and a run that stops for a budget reason is `usage_limited`, not `completed`.
* **Not a sandbox.** Tool visibility and policy decide what may be called; the
  process-level containment is the host's job.
* **Not user authentication.** An approval records a decision made by some trusted
  component. This package never authenticates a user, so a host that exposes
  approvals over HTTP must authorise the request itself.
* **Not a scheduler.** No task graph, no subagent runner, no work queue; the
  extension seams kept for one are listed in `docs/contract.md` section 10.
* **Not automatic recovery on every ledger.** The batch recovery path needs a ledger that
  can be *read* without running the call (`RecoverableLedger`). A backend that does not
  offer it keeps working on the normal path, and recovery says so and blocks instead of
  assuming nothing happened. A RUNNING orphan also has no recorded fingerprint baseline,
  so configuration or policy drift cannot be re-checked for it — only a missing tool is
  refused.
* **Not a re-run of the batch hooks.** Recovery reuses recorded results and executes what
  the ledger proves absent; `after_tool_call` is not replayed for a call whose result is
  already settled, and a host hook that cannot be reconstructed is not re-invoked.
