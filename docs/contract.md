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

Not implemented, and therefore not promised by this release: any transport of its
own (no HTTP server, no web console), a task scheduler or subagent execution, a
distributed worker pool, a plugin marketplace and production sandboxing. The core's
explicit state machine (`docs/core-state-machine-implementation-plan.zh-CN.md`, S2
onwards) is **not** implemented this round: the single existing loop is kept, and the
bridges, the harness and the durable pause/resume path are built around it. A host's
own tool gateway is supported as the delegated-ownership case
(`ownership == "host"`), not as a second implementation of this package's gateway.
Nothing here fakes the missing pieces.

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
  invocation (stream, tools, hooks, budgets, the `state_sink` commit port).
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
three different enums.

A host that used to vendor this loop under its own path (`backend/agent_core`) is
**not** a second implementation as long as its shim re-exports the same module
objects (`backend.agent_core.messages is yanxu_harness.core.messages`). That is
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
(`EXECUTION_STATE_SCHEMA_VERSION`); an unknown or future version, an unknown phase or
status and an unknown batch outcome are refused rather than guessed. The codec is
**private recovery state**: it keeps the provider continuation material a message holds,
so a host stores it with the transcript and must not publish it as an event payload.

`src/yanxu_harness/core/execution.py#pending_work_of` projects it into the question a
driver has to answer before dispatching anything: `blocking` (awaiting a decision or
evidence), `replayable` (provably never run), `declared` + `batch_outcome_unknown` (the
crash window: the model asked for these calls and no outcome was recorded — the gateway
ledger decides), and `model_request_outstanding` (an unfinished request, which is not a
tool side effect and may be asked again). `RunOutcome.pending_work` and
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

## 4. Error taxonomy

All errors derive from `HarnessError` and carry a machine-readable `code`. The
harness never inherits a host's exception classes, so a host `except
StoryStateError` cannot swallow a harness fault.

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
