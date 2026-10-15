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
