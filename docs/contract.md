# Public contract (0.1.0a2)

This document is the authority for what `yanxu_harness` promises. If code and
this file disagree, one of them is a defect; the tests in `../tests/` decide
which.

## 1. Status

Local candidate, unpublished. The source lives at
<https://github.com/SZWzz/yanxu-harness>; the distribution name `yanxu-harness` and
the import name `yanxu_harness` have not had an availability check against a package
index, and nothing has been uploaded anywhere. `0.1.0a2` means what it says: the
*shape* of the public API is deliberate and tested, and it may still change between
alpha releases, with the change recorded in the changelog. The end-to-end chain this
alpha is built for — task → tool call → preview → pause → process exit → new process
reads the status → approval → resume → only the unfinished call runs → the model
answers — is implemented and exercised by `examples/05_durable_approved_edit.py` in
real processes.

Implemented and tested in this release:

| Area | Modules |
| --- | --- |
| The neutral core (the only model loop) | `src/yanxu_harness/core/`: `agent_loop.py`, `messages.py`, `types.py`, `events.py`, `stream.py`, `queues.py`, `cancellation.py`, `tool_execution.py`, `agent.py` |
| Errors | `src/yanxu_harness/errors.py` |
| Versioning/fingerprints | `src/yanxu_harness/versioning.py` |
| Events | `src/yanxu_harness/events.py` |
| Run identity/authority/lifecycle | `src/yanxu_harness/runtime/context.py`, `src/yanxu_harness/runtime/harness.py` |
| Versioned checkpoints and resume plans | `src/yanxu_harness/runtime/checkpoint.py` |
| Tool definition/registry | `src/yanxu_harness/tools/definition.py`, `src/yanxu_harness/tools/registry.py` |
| Capability discovery | `src/yanxu_harness/tools/discovery.py` |
| Policy and approvals | `src/yanxu_harness/tools/policy.py` |
| Execution and effect states | `src/yanxu_harness/tools/execution.py`, `src/yanxu_harness/tools/outcomes.py` |
| The single execution gateway | `src/yanxu_harness/tools/gateway.py` |
| Skills (metadata/body/resource) | `src/yanxu_harness/skills/provider.py`, `src/yanxu_harness/skills/memory.py` |
| Decisions | `src/yanxu_harness/decisions/router.py` |
| Context budget/projection/summary | `src/yanxu_harness/context/policy.py` |
| Persistence ports + volatile store | `src/yanxu_harness/storage/ports.py`, `src/yanxu_harness/storage/memory.py` |
| SQLite reference store (stdlib `sqlite3`) | `src/yanxu_harness/storage/sqlite.py` |
| Local `SKILL.md` provider | `src/yanxu_harness/skills/local.py` |
| Provider port | `src/yanxu_harness/providers/protocol.py` |
| OpenAI-compatible streaming transport | `src/yanxu_harness/providers/openai_compat.py` |
| Provider → core stream bridge | `src/yanxu_harness/providers/core_bridge.py` |
| Tool → core gateway bridge | `src/yanxu_harness/tools/core_bridge.py` |
| Concrete harness lifecycle | `src/yanxu_harness/runtime/default_harness.py` |
| Adapter contract helpers | `src/yanxu_harness/testing/contract_suite.py` |
| Read-only run diagnostics | `src/yanxu_harness/diagnostics/` |
| Offline task evaluation | `src/yanxu_harness/evals/` |

Not implemented, and therefore not promised by this release: any transport of its
own (no HTTP server, no web console), a task scheduler or subagent execution, a
distributed worker pool, a plugin marketplace and production sandboxing.

The core's explicit state machine **is** implemented
(`src/yanxu_harness/core/execution.py` + `transitions.py` + `runner.py`,
`docs/core-state-machine-implementation-plan.zh-CN.md`): the reducer is a pure function,
`Runner` is the single driver that performs its actions, and there is no second model
loop. The sentence that used to stand here ("not implemented this round") was left over
from before S2 landed and is corrected here rather than deleted, so a reader who saw the
old claim can see which one is current. A host's own tool gateway is supported as the
delegated-ownership case (`ownership == "host"`), not as a second implementation of this
package's gateway. Nothing here fakes the missing pieces.

## 2. Stability levels

| Level | Meaning |
| --- | --- |
| **Public** | Re-exported from a package `__init__` and listed here. Changing it needs a changelog entry and a test change. |
| **Internal** | Everything else, including module-level helpers and names starting with `_`. Free to change between alpha releases. |
| **Persisted** | Formats that survive a restart (event schema, checkpoint schema, approval binding). Versioned independently of the package version; a reader refuses what it cannot prove safe. |

## 3. Public surfaces

### The core

`src/yanxu_harness/core/` is the only model loop. It is characterized by one set
of tests, kept in `tests/core/` in this repository and mirrored by the host that
embeds it; the two copies are the same characterization tests with the imports
pointed at this package. The behavioural contract itself is the test suite: there
is no separate architecture document in this repository to keep in sync.

The one loop is composed of four modules, all public through `yanxu_harness.core`:

* `src/yanxu_harness/core/loop_config.py#AgentLoopConfig` — the host's intent for one
  invocation (stream, tools, hooks, budgets, the `state_sink` commit port), including
  `prepare_context`: the context-preparation port (section 4.2 of the context plan) that
  produces the request view and its facts before anything is dispatched.
* `src/yanxu_harness/core/loop_config.py#RequestPreparation` — what a preparation port
  receives: the transcript after the tool-set delta, the model, the turn, the executable
  tools and the cancellation token. Plain data, no callables.
* `src/yanxu_harness/core/execution.py#ExecutionState` — where the run is, with
  `InputEvent` / `Action` / `Transition` and the versioned
  `state_to_record`/`state_from_record` codec; `pending_work_of` projects it into the
  outstanding work.
* `src/yanxu_harness/core/transitions.py#advance` — the pure reducer; `result_of`
  projects a stopped state into `LoopResult`.
* `src/yanxu_harness/core/runner.py#Runner` — the only driver: it performs the actions
  and feeds the answers back as events. `run_agent_loop`, `run_agent_loop_result`,
  `run_agent_loop_continue` and `run_agent_loop_continue_result` are wrappers over it,
  and `src/yanxu_harness/core/agent_loop.py` re-exports the same objects so an existing
  import path keeps working (`tests/core/test_core_single_runner.py` asserts the
  identity).

`src/yanxu_harness/core/outcome.py#LoopResult` is the outcome vocabulary:
`RunStatus` (how the run ended), `LoopStop` (why one invocation stopped) and
`CallOutcome` (what happened to one tool call) are three different questions and stay
three different enums. `LoopStop.CONTEXT_BUDGET_EXCEEDED` with
`CONTEXT_BUDGET_STOP_CODE` is the budget stop for "the request could not be made to fit":
distinct from `MAX_TURNS` (no turn was spent) and from a model failure (no model was asked).
`src/yanxu_harness/core/execution.py#RequestPrepared` is how a preparation result reaches
the reducer; `PrepareRequest` and `SendModelRequest` are the two actions it splits the
model request into, so the prepared view can be committed before anything is sent.

A host that used to vendor this loop under one of its own module paths (any dotted path
of the host's choosing, e.g. its own `agent_core` package) is **not** a second
implementation as long as its shim re-exports the same module objects — for example
`host_shim.messages is yanxu_harness.core.messages`. That is
what keeps `monkeypatch.setattr` on the old path, `isinstance`, `except` and
pickling working. That shim lives in the host, not here; this package ships no
compatibility file for it.

### Harness lifecycle

`src/yanxu_harness/runtime/harness.py#Harness` is a
protocol: `start`, `resume`, `cancel`, `state`.
`src/yanxu_harness/runtime/default_harness.py#DefaultHarness` is the concrete
implementation; its dependencies (`stream_fn`, tools, `Persistence`, an optional
listener) are constructor-injected, there is no global default stream, and the
identity it acts on comes from the `RunContext` the host established — a model
never establishes an identity or widens the tool set.

* `src/yanxu_harness/core/outcome.py#LoopSuspended`
  is the control signal a list-returning entry raises when a run is waiting: the
  historical contract (a list of new messages, or an exception) cannot express a pause,
  and returning messages would look like a finished answer.
* `src/yanxu_harness/runtime/harness.py#RunRecord`
  carries an optional `execution` snapshot (`core.execution.state_to_record`); a record
  without one is read from `messages`/`batch` alone, which is how records written before
  the field existed keep working. `RunOutcome.pending_work` and `RunState.pending_work`
  expose the snapshot's projection.
* `src/yanxu_harness/runtime/harness.py#validate_max_turns`
  accepts `None` (unbounded) or a positive `int`, and refuses `bool`, `0`,
  negative numbers and other types before any request, effect or lifecycle event;
  `validate_max_concurrent_tools` does the same for the tool-concurrency limit.
* `src/yanxu_harness/core/outcome.py#RunStatus`
  separates `running`, `completed`, `failed`, `cancelled`, `suspended`,
  `usage_limited` and `rejected`; `yanxu_harness.runtime` re-exports the same
  object. A turn-budget stop is `usage_limited`, never `completed`.
  `src/yanxu_harness/core/outcome.py#RUN_STATUS_TRANSITIONS` is the state machine:
  a terminal status has no outgoing edge (a finished run cannot be revived by a
  late callback or another prompt), while `running -> running` is just an update
  of the active run.
* `src/yanxu_harness/runtime/harness.py#RunOutcome`
  requires a pending interaction when suspended and a machine-readable
  `error_code` when usage-limited. It **refuses `running`**: an active run has no
  outcome, and `state()` is what answers while it is alive. `stop_code`
  (`src/yanxu_harness/core/outcome.py#LoopStop`) records why the loop stopped, so a
  host hook that stopped the run is distinguishable from a model that finished.
* `src/yanxu_harness/core/agent_loop.py#run_agent_loop_result`
  (and `run_agent_loop_continue_result`) return that structured outcome;
  `run_agent_loop`/`run_agent_loop_continue` keep their list return contract and
  are thin wrappers over the same kernel, with identical event order.
* `src/yanxu_harness/runtime/harness.py#RunRecord` is what a run persists —
  status, attempt, turns, stop reason, usage, the committed transcript, the
  request fingerprint and any recorded cancellation request — under
  `RUN_RECORD_SCHEMA_VERSION`. `state()` reads it back, so a *new* harness
  instance over the same store sees the same run. `start()` with the same request
  reuses that state, different input raises `RunRequestDrift` without touching it,
  and an already-active run raises `RunBusy` instead of starting a second loop.

### The execution state a run commits

`src/yanxu_harness/core/execution.py#ExecutionState` is where a run is, as opposed to how
it ended (`RunStatus`) or what happened to one call (`core.outcome.CallOutcome`).
`state_to_record`/`state_from_record` are its versioned codec
(`EXECUTION_STATE_SCHEMA_VERSION`, currently 2; version 1 is still read).
`SUPPORTED_EXECUTION_STATE_SCHEMA_VERSIONS` is the whole set a build may interpret: an
unknown or future version, an unknown phase or status and an unknown batch outcome are
refused rather than guessed. Version 2 adds the per-call schedule facts (`declared_index`,
`model_action_id`, `progress`, `arguments_digest`, the ledger reference, the observation
id and digest, the source attempt and the approval binding); a version-1 record simply has
none of them, which reads as "this build did not record it". The codec is
**private recovery state**: it keeps the provider continuation material a message holds,
so a host stores it with the transcript and must not publish it as an event payload.

`src/yanxu_harness/core/execution.py#pending_work_of` projects it into the question a
driver has to answer before dispatching anything: `blocking` (awaiting a decision or
evidence), `unobserved` (the driver reported no observation for these calls — the gateway
ledger decides what happened), `calls` (the per-call partition: source order, outcome,
schedule progress, ledger reference, whether a result is recorded), `replayable` (provably
never run — only claims this *outside* a dispatch window), `declared` +
`batch_outcome_unknown` (the crash window: the model asked for these calls and no outcome
was recorded), and `model_request_outstanding` (an unfinished request, which is not a tool
side effect and may be asked again). `RunOutcome.pending_work` and
`RunState.pending_work` carry the same projection, and `RunRecord.execution` stores the
snapshot; a record without one is read from `messages`/`batch` alone.

`src/yanxu_harness/core/loop_config.py#AgentLoopConfig.state_sink` is the commit port: the
runner calls it after accepting a state and **before** performing that transition's
actions, which is what makes "the model's final response is committed before its tool
calls are dispatched" an ordering property rather than a comment.

### Checkpoints and resume

`src/yanxu_harness/runtime/checkpoint.py#RunCheckpoint` is the
durable answer to "where exactly did this run stop?". Three things are stored
separately and never merged, because merging them is how a write happens twice:

* `pending_tool_calls` — the complete declaration of the batch the run stopped in;
* `executed_tool_call_ids` / `unexecuted_tool_call_ids` / `uncertain_tool_call_ids`
  — the partition of that batch, which must cover exactly the declared calls (a
  checkpoint that cannot prove the partition is refused, not partially restored).
  `uncertain_tool_call_ids` was added after schema 1: an operation whose outcome
  could not be *proven* is neither executed nor safely replayable, and collapsing it
  into either side is how a resume either writes twice or loses a real effect. A
  record written before the field existed reads it as empty, which is the only
  backwards-compatible reading;
* `pending_interactions` — the unanswered questions, with their expiry.

`src/yanxu_harness/runtime/checkpoint.py#resolve_resume`
checks the config, tool set, instructions, policy and exposure-manifest
fingerprints and **names** what drifted; a drifted tool set or policy sends the run
back for re-confirmation instead of continuing under an arrangement the author's
approval was not granted for.
`src/yanxu_harness/runtime/checkpoint.py#plan_resume` turns
that into a plan whose `replay` never contains an already-executed call, and never
an unproven one either (those are reported separately in `ResumePlan.uncertain`).

Checkpoints carry a neutral `schema_version` and deliberately **not** a host engine
identity: a host that serialized its own `engine_id` keeps doing so in its own
codec, and this type does not relocate it.

### Run context and authority

* `src/yanxu_harness/runtime/context.py#RunIdentity`
  carries namespace/session/run/attempt (plus request/operation ids).
* `src/yanxu_harness/runtime/context.py#Principal`
  may only be established server-side; `established_by` must be `"server"`.
* `src/yanxu_harness/runtime/context.py#RunContext.for_tool_call`
  derives a child context that inherits principal, cancellation and services
  unchanged. There is no constructor that builds authority from model arguments.
* `src/yanxu_harness/runtime/context.py#Cancellation`
  is cancellable from another thread, keeps the first reason, and is inherited by
  every tool call in the run.

### Tools — layer 1: definition and registration

* `src/yanxu_harness/tools/definition.py#ToolDefinition`
  declares name, version, description, effects, argument validator, handler,
  preview, execution mode, idempotency, dependencies, schema and result limit.
  An executable tool without `args_validator` is refused; a write tool without a
  `preview` is refused; `Idempotency.KEYED` without a key callable is refused.
* `src/yanxu_harness/tools/registry.py#ToolRegistry`
  refuses duplicate names, refuses a non-`ToolDefinition`, refuses a cycle among
  registered tools, and reports unregistered dependencies through
  `missing_dependencies` / `resolve_dependencies` instead of ignoring them.

### Tools — layer 2: deployment visibility

Deployment visibility is a host decision: a registered tool is not thereby
offered to a model. `src/yanxu_harness/tools/policy.py#ConservativePolicy`
denies a tool the deployment did not expose.

### Tools — layer 3: declaration and discovery

`src/yanxu_harness/tools/discovery.py#CapabilityCatalog`
lists the deployment's capabilities in stable id order and answers
`src/yanxu_harness/tools/discovery.py#CapabilityRequest`:

* a requested capability becomes **active in the next batch**, never in the batch
  that asked (`request` returns `queued`; `promote` activates),
* unknown ids and hidden ids are rejected with a reason,
* a request larger than the catalog capacity is refused **as a whole** with an
  explicit `over_capacity` reason — never silently truncated,
* ids that were not in the first batch remain discoverable later.

Discovery grants nothing: there is no code path from discovery to an approval.

### Tools — layer 4: authorization, approval, execution

`src/yanxu_harness/tools/gateway.py#ToolGateway` is the
**only** place a handler is ever invoked, and it runs the layers in a fixed
order:

```text
visibility → argument validation → policy → approval → intent → execute → settle
```

The order is part of the contract:

* visibility before validation, so a hidden tool cannot be used as a schema
  oracle;
* validation and policy before the intent, so a refused call leaves no ledger
  entry at all;
* approval before the intent, so the ledger never contains a call that was never
  authorised;
* the intent before the handler, so a crash mid-execution leaves a provable
  "may have happened" record instead of silence.

`src/yanxu_harness/tools/gateway.py#ToolInvocationResult`
reports one status per outcome, and there is no default-to-success:

| Receipt state | Reported status |
| --- | --- |
| `succeeded` | `executed` (or `replayed` when the receipt already existed) |
| `unknown` | `unknown` |
| `deferred` | `deferred` |
| `rejected` | `denied` |
| `failed` | `failed` |
| `cancelled` | `cancelled` |
| anything unsettled | `ContractError` — a backend must return a settled receipt |

Waiting is never a success: `awaiting_approval` carries the binding that is
waiting and no receipt, `deferred` means a human has to decide (the delegated
host gateway said so), and only a receipt whose state is `succeeded` counts as
an effect. `raise_for_status()` turns a refusal into the matching neutral tool
outcome, but returns `self` for `deferred`: raising there would erase the
distinction the host gateway just made. A second call with the same operation id
replays the settled receipt and does not run the handler again.

There is deliberately **no** method on the gateway that records an approval: it
reads approvals, and only the host's decision path writes them.

* `src/yanxu_harness/tools/policy.py#PolicyDecision`
  is one of `allow`, `deny`, `require_approval`.
* `src/yanxu_harness/tools/policy.py#ApprovalBinding`
  binds namespace, call id, tool name+version, argument hash, target version,
  preview hash, policy version and expiry.
  `src/yanxu_harness/tools/policy.py#check_binding`
  refuses the approval after any drift, with a distinct reason per drift
  (arguments, target version, tool contract version, preview, policy version,
  expiry).
  The binding key is the *call* (`src/yanxu_harness/tools/policy.py#binding_key`):
  namespace + call id + tool name. The bound values are deliberately not part of
  the key, because that is what lets the gateway find the earlier decision and
  say *why* it no longer applies instead of silently asking a fresh question.
  The expiry is excluded too, so extending a deadline does not create a second
  approval for the same call.
* `src/yanxu_harness/tools/policy.py#ApprovalStore`
  records decisions. Repeating the same decision is idempotent; overwriting a
  decided approval is refused. When the bound values move, `supersede` records a
  new pending question and keeps the earlier record as history. A client
  submission expresses a decision; only the store records the fact.
* `src/yanxu_harness/tools/execution.py#EffectState`
  distinguishes `queued`, `running`, `succeeded`, `failed`, `unknown`,
  `rejected`, `cancelled`, `deferred`.
  `src/yanxu_harness/tools/execution.py#should_retry`
  never retries `unknown`, and retries `failed` only for idempotent tools.
  `src/yanxu_harness/tools/execution.py#classify_failure`
  maps timeouts, transport errors and unexpected handler exceptions to `unknown`,
  and the neutral tool outcomes to their own states.
* `src/yanxu_harness/tools/execution.py#ToolIntent`
  records the operation, the namespace, the argument fingerprint and — importantly
  for delegated mode — `raw_arguments`, the model's original request. A host
  gateway fingerprints the *raw* request, so handing it the harness's validated
  argument model (whose dump fills in defaults) would make the two sides disagree
  about what was approved.
* `src/yanxu_harness/tools/execution.py#ToolExecutionBackend`
  is the single owner of intent/execute/receipt for one call, and declares its
  `ownership` as `"harness"` or `"host"`. A host gateway that keeps its own
  ledger delegates through this port; the harness does not create a second intent
  or a second approval for the same call.
* `src/yanxu_harness/tools/execution.py#RecoverableLedger`
  is the *optional* read/admission capability a ledger may offer: `observe()` answers
  `absent` / `open` / `receipt` / `unavailable` (four answers that must never be collapsed
  into two), and `admit()` decides `admitted` / `existing_open` / `existing_receipt` /
  `binding_conflict` inside one transaction. It is separate from
  `ToolExecutionBackend` so an adapter written before it keeps working: the gateway
  prefers the strong contract when the backend offers it and falls back to `begin()`
  otherwise, and a recovery that needs an answer it cannot get *blocks* rather than
  assuming nothing happened.
* `src/yanxu_harness/tools/gateway.py#ToolGateway.review` classifies one call against the
  ledger without running it (`None` means the ledger proves absence);
  `#ToolGateway.promote` records an evidenced `unknown -> succeeded/failed` convergence in
  the ledger owner, refusing any other transition. `#ToolInvocation.logical_ref()` is the
  versioned fingerprint of the logical call (`namespace`/`session`/`run`/model
  action/tool-call id), and the **derived operation id** is the same composite
  (`namespace:session:run:model_action:tool_call_id`) whenever the caller supplies a batch
  reference — so two turns that reuse a tool-call id are two operations rather than one
  operation to replay. `model_action_id` travels into the intent as well, so the ledger's
  binding check refuses a stored row whose batch differs. An explicit `operation_id` is
  never re-derived.
* `src/yanxu_harness/core/types.py#ToolSpec.recovery` is the per-tool handle a driver uses
  to ask that ledger about one call without executing it (`tools/core_bridge.ToolRecovery`).
  A tool whose adapter cannot answer carries no handle at all, which is a different fact
  from "it answered unavailable".

### Skills — three layers

* `src/yanxu_harness/skills/provider.py#SkillProvider`
  lists bounded metadata, loads one body on demand, reads one resource on demand
  and reports a fingerprint.
* `src/yanxu_harness/skills/provider.py#SkillMetadata`
  carries `when_to_use`/`avoid_when`, a version and a tool *hint*. Nothing here
  grants a permission, an approval or script execution.
* Scope isolation: a skill registered in one project scope is invisible from
  another; only `global` is shared.
* `src/yanxu_harness/skills/provider.py#safe_resource_path`
  refuses absolute paths, `..` traversal, backslashes, control characters and
  empty segments.
* `src/yanxu_harness/skills/provider.py#SkillLimits`
  bounds metadata, body, resource size and resource count; exceeding a budget is
  an error at registration, not a silent truncation.

### Decisions (optional)

* `src/yanxu_harness/decisions/router.py#DecisionRouter`
  has two shipped implementations:
  `src/yanxu_harness/decisions/router.py#NoopDecisionRouter`
  (`absent`, and a run still works) and
  `src/yanxu_harness/decisions/router.py#RuleDecisionRouter`.
* Input is only the task plus bounded candidate metadata; suggestions that name
  ids outside the offered set are rejected with a reason, duplicates collapse,
  and the request's suggestion limit is enforced
  (`src/yanxu_harness/decisions/router.py#validate_suggestions`).
* Failure is explicit: `degraded`, `cancelled` and `budget_exceeded` all carry a
  reason. `DecisionOutcome` has no field that could grant an approval or claim a
  write.

### Context

* `src/yanxu_harness/context/policy.py#ContextWindow`
  computes request capacity as window minus output reserve minus safety margin.
  An unknown window yields `None` capacity and `fits is None`; no percentage is
  invented.
* `src/yanxu_harness/context/policy.py#TranscriptArchive`
  is append-only; compaction never rewrites it.
* `src/yanxu_harness/context/policy.py#validate_publication`
  refuses a summary whose scope, source head or lease has drifted, leaving the
  active checkpoint untouched.
* `src/yanxu_harness/context/measurement.py#TokenEstimator`
  measures a *complete* request — system, every tool schema, the messages and the
  per-message/per-tool protocol overhead. A part it cannot see (an image, a custom
  block) makes the total `None` with that part named in `unmeasured`; it is never
  counted as zero. The default
  `src/yanxu_harness/context/measurement.py#HeuristicTokenEstimator` counts characters
  and says so (`exact is False`, `method` names the heuristic): it makes no claim
  about a provider's billing tokens.
* `src/yanxu_harness/context/measurement.py#RequestBudget`
  derives the capacity from the window (the output reserve is the larger of the policy
  reserve and the request's own cap, deducted **once**) and checks an independent
  `max_payload_bytes`. An unknown window, estimate or output reserve yields
  `fits is None`; a decisive failure stays `False`; `UnknownPolicy.REFUSE` turns an
  unknown into a refusal instead of a guess.
* `src/yanxu_harness/context/measurement.py#payload_budget_gate`
  is a `PayloadGate` (`(body, request) -> None`) over the final serialized body, so a
  refusal means zero transport calls. It does no I/O and starts no event loop: the
  asynchronous part of preparation cannot run in the provider's worker thread.
* `src/yanxu_harness/context/projection.py#message_fragments`
  groups a transcript into complete fragments: contiguous system messages are one
  ``instructions`` fragment, and an assistant turn absorbs the results answering the
  calls it declared (matched by id within that turn, never assumed unique across a run).
  A result nobody declared stays an ``orphan-result`` — no caller is invented for it.
* `src/yanxu_harness/context/projection.py#FragmentProjector`
  selects the smallest suffix of complete fragments that fits, drops only the oldest
  removable fragment, and refuses (`BudgetExceeded`) when the current turn or a
  host-mandated message cannot be kept. The dropped fragments' system/tool updates are
  replayed into one baseline message, and the effective prompt, sections and tool set are
  re-verified against the full transcript before the result is returned — so a revoked
  tool cannot come back. It never truncates a tool's arguments or a result's body.
* `src/yanxu_harness/context/projection.py#ProjectionResult`
  carries the provenance: scope, source head, kept/dropped fragments with reasons, the
  measurement, the request fingerprint, the effective-state fingerprint and the policy
  version. `system_state` is the canonical form of the state that gets compared.
* `src/yanxu_harness/context/archive.py#WindowedProjector`
  is the legacy character-budget view, kept for the archive-based `ContextProjector`
  protocol. Its `capacity` is a *character* capacity, it never splits a fragment, it
  honours `keep_system_entries`, and it refuses rather than returning an empty or
  instruction-less view. Token accounting belongs to `FragmentProjector`, not here.
* `src/yanxu_harness/context/controller.py#ContextController`
  prepares one request: it projects the transcript, measures it against the window and an
  independent byte limit, and returns the view plus the versioned facts about it
  (`context_schema_version` = 1: scope, source head, request/effective-state fingerprints,
  estimator identity, capacity, what was kept and dropped and why). A request that cannot
  be made to fit comes back as a *refusal*, so the loop records a budget stop instead of a
  model failure. `PreparationOutcome` is that shape. The harness wires it as
  `DefaultHarness(context_controller=...)`; scope is the run identity, bound by the harness.
* `src/yanxu_harness/runtime/harness.py#RunRecord`
  carries `context_state` (and the prepared view inside `execution.request_view`), written
  in the same transaction as the state it describes — a reader can never find the request
  view without the measurement that produced it. `context_state` is *sticky until
  replaced*: a settled batch or a pause does not erase the description of the request that
  was just sent. The transcript (`RunRecord.messages`) stays the whole conversation: a
  projection is a view, never the history.
* `src/yanxu_harness/context/artifacts.py#bounded_tool_output`
  replaces an oversized tool result with a bounded view (head, tail, exact omitted count,
  fingerprint, `artifact_ref`) **after** storing the original, never before. Nothing about
  the call changes except its text: `tool_call_id`, `tool_name`, `is_error`, `details`,
  `usage` and the arguments are untouched. A store that cannot hold the original is a
  refusal, not a dangling reference.
* `src/yanxu_harness/context/artifacts.py#InMemoryArtifactStore` declares
  `Durability.VOLATILE`; `src/yanxu_harness/storage/artifacts.py#SqliteArtifactStore` is
  the content-addressed durable one (`ARTIFACT_SCHEMA_VERSION`), with no update and no
  delete API: a committed reference must stay resolvable. `ToolOutputPolicy.require_durable`
  refuses to bound anything when the host needs references to outlive the process.
* `src/yanxu_harness/tools/artifact_read.py#artifact_read_definition`
  is how an original is read: a READ-effect tool through the gateway, taking a content id
  (never a path or URL), itself bounded and reporting which range it returned.
* `src/yanxu_harness/context/notes.py#TaskNotes`
  is the sourced task view. Every entry names its source (`NoteSource`); a model's
  suggestion renders as `suggested` and can never become a fact, an approval or a
  permission; an entry whose evidence is gone becomes `UNVERIFIED` and is never upgraded
  back. `NoteLedger` is the accumulator a host drives.
* `src/yanxu_harness/context/compaction.py#SummaryCompactor`
  summarizes a stable prefix only when the deterministic projection had to drop something
  and a host injected a `Summarizer`. The tail is never summarized, the summary travels as
  a *user* message (so it cannot enter the effective system prompt), and publication must
  still match the head it was produced for. `SummaryBudget` is absolute and
  `SummaryUsage` is committed with the facts, so a resume continues the recorded spend and
  an unconfirmed cost stays `usage_unknown` — never zero.

### Persistence

* `src/yanxu_harness/storage/ports.py#Persistence`
  provides compare-and-set saves (`StorageConflict` on drift), fenced leases
  (`src/yanxu_harness/storage/ports.py#Lease`) and ordered
  events.
* `src/yanxu_harness/storage/ports.py#Durability`
  distinguishes `durable` from `volatile`. The shipped reference store is
  volatile and says so; a cross-process restart demo must use a durable store.
* The port makes no cross-database atomicity promise: an external effect and a
  local ledger cannot share a transaction, which is why `unknown` exists.

### Providers

* `src/yanxu_harness/providers/protocol.py#Provider`
  streams neutral events and is configured entirely by the host.
* `src/yanxu_harness/providers/protocol.py#SecretReference`
  holds a secret *name*; the host resolves it. `ProviderConfig.public()` is the
  only loggable projection and contains no header values or key material.
* `ProviderEvent.private` (signatures, encrypted blobs) never enters a public
  payload; `public_payload()` drops it.
* Failure kinds are machine-readable
  (`src/yanxu_harness/providers/protocol.py#ProviderFailureKind`).

### Events

* `src/yanxu_harness/events.py#Event` carries a schema
  version, a monotonic sequence and a public payload.
* `src/yanxu_harness/events.py#assert_public_payload`
  refuses credential/header/signature-shaped keys at any depth, and refuses
  anything that is not JSON-serialisable.
* `src/yanxu_harness/runtime/envelope.py#EventEnvelope` is what a host's run
  listener receives: namespace, run, attempt, sequence, schema version, kind,
  payload and whether the event is durable. `Event` stays the store-level type;
  the envelope is the *published* one, so a consumer can tell which run and which
  attempt an event belongs to instead of inferring it.
* `src/yanxu_harness/runtime/envelope.py#EventFence` checks the envelope against
  the drive that is running now and refuses a stale, foreign or out-of-order event
  **before** anything is mutated, recording the reason. Core progress frames and
  durable events are fenced separately (they have different sequence sources).
* The listener is structural: `src/yanxu_harness/runtime/default_harness.py#DefaultHarness`
  takes `listener` (core progress frames, in-memory) and `run_listener` (committed
  envelopes). A consumer that fails is recorded in `fanout_failures` and repaired by
  `replay_run_events`, which reads the store from a cursor; nothing is rolled back
  and no tool is re-run to compensate.
* Three kinds were added for the measurement facts a report reads: `model_request_dispatched`
  (a *real* request instance reached its dispatch window — committed before the send, so it
  proves neither that the provider received it nor that a response exists),
  `model_request_completed` (the response was accepted, with the driver's monotonic duration,
  the request-body size and its named estimator, and whatever usage the provider reported),
  and `tool_call_observed` (one call's outcome and span, committed with the observation
  because the batch partition is *current state* and the next turn clears it). All three are
  validated like every other public payload: ids, statuses and numbers only, never
  arguments, results, prompts or provider material.

### Diagnostics (read-only)

* `src/yanxu_harness/diagnostics/reader.py#RunInspector` returns the facts about one run and
  performs no action: no `start`/`resume`/`cancel`, no lease, no tool invocation, no
  reconciler, no reducer advance and no side-effecting hook. Its stores are opened read-only
  (`SqlitePersistence(..., read_only=True)`, `SqliteExecutionBackend(..., read_only=True)`),
  which refuses every write path rather than promising not to use it; the schema version is
  still checked, so a newer layout is refused instead of misread. `run_id` is not permission:
  the record's namespace and session must match the identity presented, and ledger rows are
  scoped by namespace *and* run.
* `src/yanxu_harness/diagnostics/projection.py#project_report` is a pure function of the facts
  and the boundary that produced them. It labels its own conclusions (`DERIVED`) and states
  what is missing, conflicting or unsupported instead of inferring it.
* `src/yanxu_harness/diagnostics/types.py#RunDiagnosticReport` is versioned
  (`DIAGNOSTIC_SCHEMA_VERSION`) with `TraceEntry`, `SpanObservation`, `UsageObservation`,
  `CallDiagnostic` and `ReadBoundary`. Three rules are enforced by construction: an unknown
  measurement carries **no** amount (a zero would be a claim), a duration needs two recorded
  endpoints read by one monotonic clock, and an entry whose attempt was never recorded says
  `unknown`/`legacy` rather than borrowing the reader's attempt.
* `src/yanxu_harness/diagnostics/render.py` renders one report as safe JSON (the fact format)
  or Markdown (a derived view of the same report). Markdown escapes free text, so a record's
  detail string cannot inject markup or break out of a table cell.
* `src/yanxu_harness/diagnostics/render.py#sanitise_text` bounds and strips every free-text
  value the report publishes — in payload values, in a run's `detail`, and inside a nested
  usage-like mapping. A name-based blacklist is not enough (a secret is usually a *value*), so
  the shapes are matched: `Bearer …`, `sk-…`/`xox…`/`gh?_…`/`AKIA…`, a credential assigned
  anywhere in ordinary text (`password=…`, `api_key: …`, `key=sk-…`), `user:pass@host` and query
  strings, plus absolute paths, and the text is capped so an exception body or a file's contents
  cannot travel. An ordinary refusal reason passes through readable.
* `python -m yanxu_harness.diagnostics inspect --runs <db> [--operations <db>] --namespace N
  --session S --run-id R [--format json|markdown] [-o FILE]`: the read-only CLI. It refuses a
  missing store instead of creating one and never migrates a schema.

### Evaluation

* `src/yanxu_harness/evals/types.py#EvalCase` is a frozen question: case and version, the
  task, the fixture with its content digest **and the command its own tests are run with**,
  the tool/policy configuration, the independent verifier and its digest, the turn/runtime
  budgets, the fault injections allowed, the model — and the acceptance truth
  (`expectations`) the verifier decides from. `EvalSuite` digests its cases, so two runs are
  comparable only when they answer the same question. Nested values are owned immutably, so a
  frozen case cannot be changed from outside after its digest was taken.
* **The question identity is versioned.** `EVAL_DIGEST_ALGORITHM` is 2; algorithm 1 covered
  everything above except `expectations` and the fixture's `baseline_command`. A record that
  carries no algorithm is read as algorithm 1: still readable, but the comparator reports it as
  `legacy` and refuses to compare it, because it cannot establish that two sides answered the
  same question (the defect the acceptance review reproduces as RDE-E1). An unknown algorithm
  is refused when the record is read.
* `src/yanxu_harness/evals/verifiers.py#VERIFIERS` are the acceptance criteria: each reads the
  workspace, the stores and the fixture's own test command, and returns evidence with its
  verdict. A verifier that cannot run returns an infrastructure error. Its source is digested
  into the case, so editing a criterion makes earlier results incomparable rather than
  quietly comparable.
* `src/yanxu_harness/evals/runner.py#run_case` drives the real harness (one isolated directory
  per case and repeat, the real SQLite stores, the real Gateway) and classifies the outcome:
  the verifier's verdict is `passed`/`failed`, the framework's own failure is `infra_error`, a
  case whose premise does not hold is `invalid_result`, and an unrun or interrupted case is
  `not_run`/`cancelled`. Those categories are never averaged together.
* `src/yanxu_harness/evals/compare.py#compare_reports` matches on the case identity —
  `case_id`, version, **digest algorithm** and digest — keeps a baseline failure a failure,
  lists incomparable cases with the reason, and reports quality, reliability and cost
  separately — cost as a known subtotal with its coverage, never as a total that hides what was
  not measured. A result whose digest is missing, malformed, written by an unknown algorithm,
  or contradicts the suite record it came with is listed as incomparable instead of being
  trusted; a digest string in a JSON file is checked, not believed.
* `python -m yanxu_harness.evals run|compare ...`: offline by default. A real-model run must
  be requested by name with repeats and a budget, and without a configured adapter those cases
  are reported `not_run` rather than answered by the scripted adapter. The committed samples
  under `docs/samples/evals` are regenerated by
  `python scripts/regenerate_eval_samples.py`, which also produces the deliberate
  wrong-answer baseline the comparison sample needs.
* What the evaluation does **not** provide: an OS sandbox. A case's workspace is an isolated
  directory and the fixture's command runs with an explicit argv, cwd, timeout and bounded
  output, but a tool that chose to reach outside its workspace could — path containment is a
  policy, not an operating-system boundary. Real isolation (containers, seccomp, a separate
  user) is the host's to provide, and the plan says so.

## 4. Error taxonomy

All errors derive from `HarnessError` and carry a machine-readable `code`. The
harness never inherits a host's exception classes, so a host `except
StoryStateError` cannot swallow a harness fault.

Two errors carry structured detail beyond their code, because a caller has to be able to audit
the decision rather than only see that one was made:

* `src/yanxu_harness/errors.py#ResumeRefused` (a `ContractError`, `code == "resume_refused"`) is
  raised when a resume is refused — drifted arrangement (`reason_code == "arrangement_drift"`),
  or no recorded baseline (`"no_arrangement_baseline"`). It carries the machine-readable reason,
  the drifted dimensions and **both** fingerprint sets that were compared, and
  `durable = False` with `to_record()` for a host that wants to log it: nothing was written to
  the run (the point is that the record stays exactly as resumable as it was), so the refusal is
  reported to the caller rather than persisted. A host with legitimate write rights can record
  it as an audit fact at its own boundary;
* `src/yanxu_harness/errors.py#RunRequestDrift` refuses a second submission that carries
  different input for one run identity.

| Error | Meaning |
| --- | --- |
| `ConfigurationError` | The host configured something the harness cannot honour. |
| `ContractError` | A registration, definition, payload or event broke a stated contract. |
| `VersionRefused` | A versioned artifact is unknown or future; nothing is guessed. |
| `BudgetExceeded` | A declared budget was hit; the request was not dispatched. |
| `ProviderFailure` | The provider failed, timed out or produced invalid output. |
| `OutcomeUnknown` | An external effect cannot be proven; never auto-retried. |
| `StorageConflict` | A CAS, fence or lease check refused the write. |
| `SkillNotFound`, `SkillResourceDenied`, `SkillBudgetExceeded` | Skill lookups, paths and budgets. |

Tool-call **results** are a different thing from errors, because a refused call is
something the model must see rather than something the process raises. They live
in `src/yanxu_harness/tools/outcomes.py`:
`ToolRejected` (refused or failed), `ToolArgsInvalid` (fix the arguments and
try again), `ToolDeferred` (waiting for a human), `ToolCancelled`. There is
deliberately only one such vocabulary in the package: two sets of `Tool*`
classes is how `except` clauses start silently missing things.

## 5. Versions and fingerprints

* `src/yanxu_harness/versioning.py#fingerprint` hashes the
  canonical JSON of a value (`sha256:<hex>`, sorted keys, `ensure_ascii=False`).
* `src/yanxu_harness/versioning.py#require_version`
  refuses future and unknown versions; the supported sets live in
  `SUPPORTED_EVENT_VERSIONS` and `SUPPORTED_CHECKPOINT_VERSIONS`.
* Approval bindings, decision caches and summary publications all key off
  fingerprints, so a version/source/policy change invalidates the cached fact
  instead of reusing it.

The persisted formats this build writes, and what it still reads:

| Format | Written | Read | Refusal rule |
| --- | --- | --- | --- |
| Run record (`RUN_RECORD_SCHEMA_VERSION`) | 1 | 1 | unknown version refused |
| Execution state (`EXECUTION_STATE_SCHEMA_VERSION`) | 4 | 1, 2, 3, 4 | unknown version refused (`VersionRefused`); a version-1/2 record keeps its per-call outcome and only the ledger may say what ran |
| Context/projection facts (`CONTEXT_STATE_SCHEMA_VERSION`) | 1 | 1 | unknown version refused |
| Summary facts (`SUMMARY_FACTS_VERSION`) | 2 | 2 | a legacy prior is read only conservatively (counts are never taken across runs) |
| Diagnostic report (`DIAGNOSTIC_SCHEMA_VERSION`) | 1 | 1 | unknown version refused |
| Eval case/result (`EVAL_SCHEMA_VERSION`) | 1 | 1 | unknown version refused |
| Harness storage schema (`storage/sqlite.py#SCHEMA_VERSION`) | current | current | a database from a newer build is refused, never "read hopefully" |

Version 4 of the execution state adds the diagnostic measurements: the outstanding model
dispatch (`request_instance`, `request_started_at`), the single pending measurement
(`last_request_measurement`), and per-call timing (`BatchCallState.started_at` /
`duration_ms`). Version 3 added `ExecutionState.arrangement` — the deployment a batch was
declared under, recorded *before* the first dispatch. Version 2 added the per-call
settlement fields. A record read from an older version never has values invented for the
fields it lacks: the missing baseline is a refusal, the missing measurement is reported as
unknown, and the missing per-call claim is left to the operation ledger.

## 6. Dependency direction

Application → harness → neutral ports / standard library / explicitly chosen
extras. The base install has an empty dependency list; `pydantic`, `yaml` and
`dev` are extras. The fence is enforced by
`tests/test_import_fence.py`, which also imports every
submodule in a cold process and asserts that no forbidden top-level module
appeared.

## 7. Status transitions

`src/yanxu_harness/core/outcome.py#RUN_STATUS_TRANSITIONS` is the table, and
`assert_transition` is what enforces it. A terminal status has no outgoing edge:
a finished run cannot be revived by a late callback or another prompt. Reaching
`SUSPENDED` does not end the run's logical identity — the attempt advances instead.

```text
RUNNING      → RUNNING | COMPLETED | FAILED | CANCELLED | SUSPENDED | USAGE_LIMITED | REJECTED
SUSPENDED    → RUNNING | SUSPENDED | COMPLETED | FAILED | CANCELLED | REJECTED | USAGE_LIMITED
COMPLETED    → (none)
FAILED       → (none)
CANCELLED    → (none)
USAGE_LIMITED→ (none)
REJECTED     → (none)
```

`RUNNING → RUNNING` is an update of an active run (turns, usage); `SUSPENDED →
SUSPENDED` is a *second* pause under a new attempt, not the same fact repeated.
`SUSPENDED → USAGE_LIMITED` is a resumed attempt that meets the same logical run's turn
budget before it can answer: the attempt cannot continue, and none of the alternatives is
honest (``COMPLETED`` would claim an answer nobody produced, staying ``SUSPENDED`` would
look resumable).
`LoopStop` (`src/yanxu_harness/core/outcome.py#LoopStop`) records why one
invocation stopped, which is a different question from how the run ended:
`ASSISTANT_FINAL` and `STOP_HOOK` both end as `COMPLETED`, and a caller can tell
them apart without reading the answer's text. `MODEL_ERROR` (the provider
failed) and `INTERNAL_ERROR` (a hook, bridge or driver raised) are likewise kept
apart, so retrying a provider error never re-runs a broken host hook.

A pause is an outcome, not an exception and not a completion. The structured
entries (`run_agent_loop_result`, `Agent.prompt_result`, `Agent.continue_run_result`)
return `LoopResult(status=SUSPENDED)` with `pending_interaction`; the historical
list-returning entries cannot express that, so they raise
`src/yanxu_harness/core/outcome.py#LoopSuspended` carrying the same readable
result instead of returning a list that looks like a finished answer.

## 8. Identity and the approval trust boundary

Three rules, because each of them is a way a harness leaks authority:

* **Identity comes from the host.** `RunContext` carries namespace/session/run and
  the `Principal`; `Principal.established_by` must be `"server"`, and there is no
  constructor that builds authority from an untrusted mapping. A tool call's child
  context inherits the principal and the cancellation token unchanged.
* **A decision is a fact in the approval store, not a value in a request.**
  `resume(run_id, resolution)` accepts hints for audit and nothing else:
  `resume(run_id, {"approved": True})` changes nothing, and a test asserts exactly
  that. The gateway reads the `ApprovalStore`, and the package deliberately has no
  method that records an approval — that belongs to the host's decision path.
* **Waiting is not success.** `awaiting_approval`, `deferred` and `unknown` are
  reported as themselves; they stop the run before the next model request and are
  never written as a completed tool result handed to the model.

## 9. What this contract does **not** claim

* **A tool policy is not an operating-system sandbox.** Visibility and policy decide
  what may be called; process containment, filesystem limits and network isolation
  are the host's responsibility.
* **An approval record is not user authentication.** It records a decision that some
  trusted component made. This package never authenticates a user, and the example's
  `approve` command is a local operator entry.
* **No exactly-once for arbitrary external effects.** The package makes an
  unprovable outcome visible (`unknown`) and refuses to retry it automatically; it
  cannot promise that a second call is impossible for a system it does not control.
* **No cross-database transaction.** The run store, the operation ledger and the
  approval ledger may be separate files or separate databases; recovery works from
  references and receipts, not from a distributed commit.

## 10. Growing towards multiple tasks

Kept deliberately small, and only where something already calls it:

* a run is identified by (namespace, session, run, attempt) and carries its own
  budget, cancellation token and events, so several runs can share one process
  without sharing state (`tests/test_runtime_isolation.py` runs two independent
  harnesses over one store);
* `ToolSpec.replay` and `ToolDefinition.idempotency` are the per-tool hooks a future
  scheduler would read; the harness already uses them to decide that a settled
  operation is replayed rather than re-executed;
* `should_stop_after_turn` and `should_suspend_after_batch` are the loop's two policy
  seams, and `max_runtime_seconds` / `lease_ttl_seconds` the two resource seams.

There is **no** scheduler, no task graph, no subagent runner and no work queue: an
interface with a caller today is kept, and one without is not invented.

## 11. What the tests prove

`tests/` runs without any other repository, without a
network and without a model. It covers the tool contract, the discovery timing
rule, the approval invalidation matrix, the `unknown` retry rule, skill scopes
and path confinement, decision validation, context arithmetic, storage CAS/fence
semantics, provider secrecy and the import fence. Host adapters can run the same
checks through `src/yanxu_harness/testing/contract_suite.py`.

`docs/verification.md` maps each failure window to the test that exercises it and
records how the evidence was produced (a real child process, a real store, the
handler's own append-only log). It also lists what is deliberately **not**
verified — 3.10.14, 3.11.15 and 3.12.13 are executed, while Linux and Windows are
not, and no real model provider is called by any automated check.
