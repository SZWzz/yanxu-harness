# Public contract (0.1.0a1)

This document is the authority for what `yanxu_harness` promises. If code and
this file disagree, one of them is a defect; the tests in `../tests/` decide
which.

## 1. Status

Local candidate, unpublished. The distribution name `yanxu-harness` and the
import name `yanxu_harness` are unverified placeholders. `0.1.0a1` means what it
says: the *shape* of the public API is deliberate and tested, and it may still
change between alpha releases, with the change recorded in the changelog.

Implemented and tested in this release:

| Area | Modules |
| --- | --- |
| The neutral core (the only model loop) | `packages/agent-harness/src/yanxu_harness/core/`: `agent_loop.py`, `messages.py`, `types.py`, `events.py`, `stream.py`, `queues.py`, `cancellation.py`, `tool_execution.py`, `agent.py` |
| Errors | `packages/agent-harness/src/yanxu_harness/errors.py` |
| Versioning/fingerprints | `packages/agent-harness/src/yanxu_harness/versioning.py` |
| Events | `packages/agent-harness/src/yanxu_harness/events.py` |
| Run identity/authority/lifecycle | `packages/agent-harness/src/yanxu_harness/runtime/context.py`, `packages/agent-harness/src/yanxu_harness/runtime/harness.py` |
| Versioned checkpoints and resume plans | `packages/agent-harness/src/yanxu_harness/runtime/checkpoint.py` |
| Tool definition/registry | `packages/agent-harness/src/yanxu_harness/tools/definition.py`, `packages/agent-harness/src/yanxu_harness/tools/registry.py` |
| Capability discovery | `packages/agent-harness/src/yanxu_harness/tools/discovery.py` |
| Policy and approvals | `packages/agent-harness/src/yanxu_harness/tools/policy.py` |
| Execution and effect states | `packages/agent-harness/src/yanxu_harness/tools/execution.py`, `packages/agent-harness/src/yanxu_harness/tools/outcomes.py` |
| The single execution gateway | `packages/agent-harness/src/yanxu_harness/tools/gateway.py` |
| Skills (metadata/body/resource) | `packages/agent-harness/src/yanxu_harness/skills/provider.py`, `packages/agent-harness/src/yanxu_harness/skills/memory.py` |
| Decisions | `packages/agent-harness/src/yanxu_harness/decisions/router.py` |
| Context budget/projection/summary | `packages/agent-harness/src/yanxu_harness/context/policy.py` |
| Persistence ports + volatile store | `packages/agent-harness/src/yanxu_harness/storage/ports.py`, `packages/agent-harness/src/yanxu_harness/storage/memory.py` |
| Provider port | `packages/agent-harness/src/yanxu_harness/providers/protocol.py` |
| Adapter contract helpers | `packages/agent-harness/src/yanxu_harness/testing/contract_suite.py` |

Not implemented yet, and therefore not promised by this release: the concrete run-level `Harness` implementation (the core's loop is reachable, but the host still drives it through its own runner), the SQLite reference store, the local `SKILL.md` provider, and any transport. Those land in later phases; until they exist, a host supplies its own runner and adapters. The host's existing tool gateway is one such adapter: it is the delegated-ownership case (`ownership == "host"`), not a second implementation of this package's gateway. Nothing here fakes the missing pieces.

## 2. Stability levels

| Level | Meaning |
| --- | --- |
| **Public** | Re-exported from a package `__init__` and listed here. Changing it needs a changelog entry and a test change. |
| **Internal** | Everything else, including module-level helpers and names starting with `_`. Free to change between alpha releases. |
| **Persisted** | Formats that survive a restart (event schema, checkpoint schema, approval binding). Versioned independently of the package version; a reader refuses what it cannot prove safe. |

## 3. Public surfaces

### The core

`packages/agent-harness/src/yanxu_harness/core/` is the only model loop. Its
behaviour is specified by the host's behavioural contract
(`docs/architecture/agent-core-contract.md`) and characterized by both test
suites: the host's `tests/agent_core/` and this package's
`packages/agent-harness/tests/core/`, which are the same characterization tests
with the imports pointed at the package.

The host's old path `backend/agent_core` is **not** a second implementation: its
`__init__.py` registers the same module objects under the old names
(`backend.agent_core.messages is yanxu_harness.core.messages`). That is what keeps
`monkeypatch.setattr` on the old path, `isinstance`, `except` and pickling
working, and it is why there is no shim file to edit by accident.

### Harness lifecycle

`packages/agent-harness/src/yanxu_harness/runtime/harness.py#Harness` is a
protocol: `start`, `resume`, `cancel`, `state`.

* `packages/agent-harness/src/yanxu_harness/runtime/harness.py#validate_max_turns`
  accepts `None` (unbounded) or a positive `int`, and refuses `bool`, `0`,
  negative numbers and other types before any request, effect or lifecycle event.
* `packages/agent-harness/src/yanxu_harness/runtime/harness.py#RunStatus`
  separates `completed`, `failed`, `cancelled`, `suspended`, `usage_limited` and
  `rejected`. A turn-budget stop is `usage_limited`, never `completed`.
* `packages/agent-harness/src/yanxu_harness/runtime/harness.py#RunOutcome`
  requires a pending interaction when suspended and a machine-readable
  `error_code` when usage-limited.

### Checkpoints and resume

`packages/agent-harness/src/yanxu_harness/runtime/checkpoint.py#RunCheckpoint` is the
durable answer to "where exactly did this run stop?". Three things are stored
separately and never merged, because merging them is how a write happens twice:

* `pending_tool_calls` — the complete declaration of the batch the run stopped in;
* `executed_tool_call_ids` / `unexecuted_tool_call_ids` — the partition of that
  batch, which must cover exactly the declared calls (a checkpoint that cannot
  prove the partition is refused, not partially restored);
* `pending_interactions` — the unanswered questions, with their expiry.

`packages/agent-harness/src/yanxu_harness/runtime/checkpoint.py#resolve_resume`
checks the config, tool set, instructions, policy and exposure-manifest
fingerprints and **names** what drifted; a drifted tool set or policy sends the run
back for re-confirmation instead of continuing under an arrangement the author's
approval was not granted for.
`packages/agent-harness/src/yanxu_harness/runtime/checkpoint.py#plan_resume` turns
that into a plan whose `replay` never contains an already-executed call.

Checkpoints carry a neutral `schema_version` and deliberately **not** a host engine
identity: a host that serialized its own `engine_id` keeps doing so in its own
codec, and this type does not relocate it.

### Run context and authority

* `packages/agent-harness/src/yanxu_harness/runtime/context.py#RunIdentity`
  carries namespace/session/run/attempt (plus request/operation ids).
* `packages/agent-harness/src/yanxu_harness/runtime/context.py#Principal`
  may only be established server-side; `established_by` must be `"server"`.
* `packages/agent-harness/src/yanxu_harness/runtime/context.py#RunContext.for_tool_call`
  derives a child context that inherits principal, cancellation and services
  unchanged. There is no constructor that builds authority from model arguments.
* `packages/agent-harness/src/yanxu_harness/runtime/context.py#Cancellation`
  is cancellable from another thread, keeps the first reason, and is inherited by
  every tool call in the run.

### Tools — layer 1: definition and registration

* `packages/agent-harness/src/yanxu_harness/tools/definition.py#ToolDefinition`
  declares name, version, description, effects, argument validator, handler,
  preview, execution mode, idempotency, dependencies, schema and result limit.
  An executable tool without `args_validator` is refused; a write tool without a
  `preview` is refused; `Idempotency.KEYED` without a key callable is refused.
* `packages/agent-harness/src/yanxu_harness/tools/registry.py#ToolRegistry`
  refuses duplicate names, refuses a non-`ToolDefinition`, refuses a cycle among
  registered tools, and reports unregistered dependencies through
  `missing_dependencies` / `resolve_dependencies` instead of ignoring them.

### Tools — layer 2: deployment visibility

Deployment visibility is a host decision: a registered tool is not thereby
offered to a model. `packages/agent-harness/src/yanxu_harness/tools/policy.py#ConservativePolicy`
denies a tool the deployment did not expose.

### Tools — layer 3: declaration and discovery

`packages/agent-harness/src/yanxu_harness/tools/discovery.py#CapabilityCatalog`
lists the deployment's capabilities in stable id order and answers
`packages/agent-harness/src/yanxu_harness/tools/discovery.py#CapabilityRequest`:

* a requested capability becomes **active in the next batch**, never in the batch
  that asked (`request` returns `queued`; `promote` activates),
* unknown ids and hidden ids are rejected with a reason,
* a request larger than the catalog capacity is refused **as a whole** with an
  explicit `over_capacity` reason — never silently truncated,
* ids that were not in the first batch remain discoverable later.

Discovery grants nothing: there is no code path from discovery to an approval.

### Tools — layer 4: authorization, approval, execution

`packages/agent-harness/src/yanxu_harness/tools/gateway.py#ToolGateway` is the
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

`packages/agent-harness/src/yanxu_harness/tools/gateway.py#ToolInvocationResult`
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

* `packages/agent-harness/src/yanxu_harness/tools/policy.py#PolicyDecision`
  is one of `allow`, `deny`, `require_approval`.
* `packages/agent-harness/src/yanxu_harness/tools/policy.py#ApprovalBinding`
  binds namespace, call id, tool name+version, argument hash, target version,
  preview hash, policy version and expiry.
  `packages/agent-harness/src/yanxu_harness/tools/policy.py#check_binding`
  refuses the approval after any drift, with a distinct reason per drift
  (arguments, target version, tool contract version, preview, policy version,
  expiry).
  The binding key is the *call* (`packages/agent-harness/src/yanxu_harness/tools/policy.py#binding_key`):
  namespace + call id + tool name. The bound values are deliberately not part of
  the key, because that is what lets the gateway find the earlier decision and
  say *why* it no longer applies instead of silently asking a fresh question.
  The expiry is excluded too, so extending a deadline does not create a second
  approval for the same call.
* `packages/agent-harness/src/yanxu_harness/tools/policy.py#ApprovalStore`
  records decisions. Repeating the same decision is idempotent; overwriting a
  decided approval is refused. When the bound values move, `supersede` records a
  new pending question and keeps the earlier record as history. A client
  submission expresses a decision; only the store records the fact.
* `packages/agent-harness/src/yanxu_harness/tools/execution.py#EffectState`
  distinguishes `queued`, `running`, `succeeded`, `failed`, `unknown`,
  `rejected`, `cancelled`, `deferred`.
  `packages/agent-harness/src/yanxu_harness/tools/execution.py#should_retry`
  never retries `unknown`, and retries `failed` only for idempotent tools.
  `packages/agent-harness/src/yanxu_harness/tools/execution.py#classify_failure`
  maps timeouts, transport errors and unexpected handler exceptions to `unknown`,
  and the neutral tool outcomes to their own states.
* `packages/agent-harness/src/yanxu_harness/tools/execution.py#ToolIntent`
  records the operation, the namespace, the argument fingerprint and — importantly
  for delegated mode — `raw_arguments`, the model's original request. A host
  gateway fingerprints the *raw* request, so handing it the harness's validated
  argument model (whose dump fills in defaults) would make the two sides disagree
  about what was approved.
* `packages/agent-harness/src/yanxu_harness/tools/execution.py#ToolExecutionBackend`
  is the single owner of intent/execute/receipt for one call, and declares its
  `ownership` as `"harness"` or `"host"`. A host gateway that keeps its own
  ledger delegates through this port; the harness does not create a second intent
  or a second approval for the same call.

### Skills — three layers

* `packages/agent-harness/src/yanxu_harness/skills/provider.py#SkillProvider`
  lists bounded metadata, loads one body on demand, reads one resource on demand
  and reports a fingerprint.
* `packages/agent-harness/src/yanxu_harness/skills/provider.py#SkillMetadata`
  carries `when_to_use`/`avoid_when`, a version and a tool *hint*. Nothing here
  grants a permission, an approval or script execution.
* Scope isolation: a skill registered in one project scope is invisible from
  another; only `global` is shared.
* `packages/agent-harness/src/yanxu_harness/skills/provider.py#safe_resource_path`
  refuses absolute paths, `..` traversal, backslashes, control characters and
  empty segments.
* `packages/agent-harness/src/yanxu_harness/skills/provider.py#SkillLimits`
  bounds metadata, body, resource size and resource count; exceeding a budget is
  an error at registration, not a silent truncation.

### Decisions (optional)

* `packages/agent-harness/src/yanxu_harness/decisions/router.py#DecisionRouter`
  has two shipped implementations:
  `packages/agent-harness/src/yanxu_harness/decisions/router.py#NoopDecisionRouter`
  (`absent`, and a run still works) and
  `packages/agent-harness/src/yanxu_harness/decisions/router.py#RuleDecisionRouter`.
* Input is only the task plus bounded candidate metadata; suggestions that name
  ids outside the offered set are rejected with a reason, duplicates collapse,
  and the request's suggestion limit is enforced
  (`packages/agent-harness/src/yanxu_harness/decisions/router.py#validate_suggestions`).
* Failure is explicit: `degraded`, `cancelled` and `budget_exceeded` all carry a
  reason. `DecisionOutcome` has no field that could grant an approval or claim a
  write.

### Context

* `packages/agent-harness/src/yanxu_harness/context/policy.py#ContextWindow`
  computes request capacity as window minus output reserve minus safety margin.
  An unknown window yields `None` capacity and `fits is None`; no percentage is
  invented.
* `packages/agent-harness/src/yanxu_harness/context/policy.py#TranscriptArchive`
  is append-only; compaction never rewrites it.
* `packages/agent-harness/src/yanxu_harness/context/policy.py#validate_publication`
  refuses a summary whose scope, source head or lease has drifted, leaving the
  active checkpoint untouched.

### Persistence

* `packages/agent-harness/src/yanxu_harness/storage/ports.py#Persistence`
  provides compare-and-set saves (`StorageConflict` on drift), fenced leases
  (`packages/agent-harness/src/yanxu_harness/storage/ports.py#Lease`) and ordered
  events.
* `packages/agent-harness/src/yanxu_harness/storage/ports.py#Durability`
  distinguishes `durable` from `volatile`. The shipped reference store is
  volatile and says so; a cross-process restart demo must use a durable store.
* The port makes no cross-database atomicity promise: an external effect and a
  local ledger cannot share a transaction, which is why `unknown` exists.

### Providers

* `packages/agent-harness/src/yanxu_harness/providers/protocol.py#Provider`
  streams neutral events and is configured entirely by the host.
* `packages/agent-harness/src/yanxu_harness/providers/protocol.py#SecretReference`
  holds a secret *name*; the host resolves it. `ProviderConfig.public()` is the
  only loggable projection and contains no header values or key material.
* `ProviderEvent.private` (signatures, encrypted blobs) never enters a public
  payload; `public_payload()` drops it.
* Failure kinds are machine-readable
  (`packages/agent-harness/src/yanxu_harness/providers/protocol.py#ProviderFailureKind`).

### Events

* `packages/agent-harness/src/yanxu_harness/events.py#Event` carries a schema
  version, a monotonic sequence and a public payload.
* `packages/agent-harness/src/yanxu_harness/events.py#assert_public_payload`
  refuses credential/header/signature-shaped keys at any depth, and refuses
  anything that is not JSON-serialisable.

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
in `packages/agent-harness/src/yanxu_harness/tools/outcomes.py`:
`ToolRejected` (refused or failed), `ToolArgsInvalid` (fix the arguments and
try again), `ToolDeferred` (waiting for a human), `ToolCancelled`. There is
deliberately only one such vocabulary in the package: two sets of `Tool*`
classes is how `except` clauses start silently missing things.

## 5. Versions and fingerprints

* `packages/agent-harness/src/yanxu_harness/versioning.py#fingerprint` hashes the
  canonical JSON of a value (`sha256:<hex>`, sorted keys, `ensure_ascii=False`).
* `packages/agent-harness/src/yanxu_harness/versioning.py#require_version`
  refuses future and unknown versions; the supported sets live in
  `SUPPORTED_EVENT_VERSIONS` and `SUPPORTED_CHECKPOINT_VERSIONS`.
* Approval bindings, decision caches and summary publications all key off
  fingerprints, so a version/source/policy change invalidates the cached fact
  instead of reusing it.

## 6. Dependency direction

Application → harness → neutral ports / standard library / explicitly chosen
extras. The base install has an empty dependency list; `pydantic`, `yaml` and
`dev` are extras. The fence is enforced by
`packages/agent-harness/tests/test_import_fence.py`, which also imports every
submodule in a cold process and asserts that no forbidden top-level module
appeared.

## 7. What the tests prove

`packages/agent-harness/tests/` runs without the host repository, without a
network and without a model. It covers the tool contract, the discovery timing
rule, the approval invalidation matrix, the `unknown` retry rule, skill scopes
and path confinement, decision validation, context arithmetic, storage CAS/fence
semantics, provider secrecy and the import fence. Host adapters can run the same
checks through `packages/agent-harness/src/yanxu_harness/testing/contract_suite.py`.
