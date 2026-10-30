# Adapter guide

The harness talks to your application through six ports. This page says what each
one must do, what it must not do, and which checks you can run against your own
implementation instead of taking "it should work" on trust.

Run the shared checks first — they do not import pytest and they never touch your
application code:

```python
from yanxu_harness.testing import (
    check_persistence_contract,
    check_skill_provider_contract,
    check_unknown_is_terminal,
)

observed = await check_persistence_contract(my_store)                       # CAS, fences, ordering
observed = await check_skill_provider_contract(my_provider, skill_id="…", scope="…")
check_unknown_is_terminal()                                                # the retry rule
```

Each returns the list of checks it observed, so a report can name them. They
raise `AssertionError` on the first violation.

## 0. Wiring the harness: what a host supplies

The ports below are what an adapter implements. This section is what a host *passes
to* `DefaultHarness`, which is the smallest complete integration (section 7 covers the
optional managed-context inputs, which are a host decision each):

| You supply | Where it goes | Why it must come from you |
| --- | --- | --- |
| a provider bridge | `stream_fn=CoreStreamBridge(provider)` | the bridge is provider-neutral; the endpoint, model and credentials are yours |
| the model-visible tools | `tools=[core_tool(definition, bindings), …]` | a registered tool is not thereby visible; the deployment decides |
| a store | `persistence=SqlitePersistence(path)` (or your own `Persistence`) | durability is a property of your storage, not of this package |
| identity | `RunContext(identity=…, principal=…)` on every call | authority is established server-side and never derived from model output |
| (optional) policy seams | `stop_after_turn`, `suspend_after_batch`, `fingerprints`, `reconcile`, `run_listener` | stopping, approving and reconciling are host decisions |
| (optional) context preparation | `context_controller=ContextController` | measuring a request means knowing your model's window, your tokenizer and your storage — see section 7 |

```python
from yanxu_harness.providers import CoreStreamBridge, OpenAICompatibleProvider, ProviderConfig, SecretReference
from yanxu_harness.runtime import DefaultHarness
from yanxu_harness.tools import (
    ConservativePolicy, GatewayBindings, ToolGateway, ToolRegistry, core_tool,
)

gateway = ToolGateway(
    registry=ToolRegistry([my_definition]),
    policy=ConservativePolicy(),                 # writes need an approval
    approvals=SqliteApprovalStore("approvals.db"),
    backend=SqliteExecutionBackend("operations.db"),
)
bindings = GatewayBindings(
    gateway=gateway,
    context=lambda: current_run_context(),        # resolved per call: a resume has a new attempt
    target_version=lambda call_id, args: read_current_version(args["path"]),
)
harness = DefaultHarness(
    stream_fn=CoreStreamBridge(OpenAICompatibleProvider(config, secrets=my_secret_source)),
    tools=[core_tool(my_definition, bindings)],
    persistence=SqlitePersistence("runs.db"),
    fingerprints={"policy": "my-policy-v3"},   # optional: a policy identity you own
)
```

Four integration points deserve their own note, because each is a place where a host
usually wants to shortcut and must not (the managed-context inputs have their own four in
section 7):

* **The business version check stays yours.** `target_version` is a callback into
  *your* store, read at call time, and the approval binds to what it returns. Do not
  accept a version from the tool arguments as the thing to compare against; the model
  may repeat a version but never establishes one. The write itself should be a
  conditional update (`UPDATE ... WHERE version = ?`) so the check is the database's.
* **Permissions stay yours.** `ConservativePolicy` is a default, not a policy engine:
  deploy a tool only when the current principal may use it, and answer
  `PolicyKind.DENY` otherwise. `Principal.permissions` is what your policy reads; a
  skill's `allowed-tools` list and a model's arguments are not permissions.
* **Reconciliation stays yours.** An operation whose outcome could not be proven is
  never settled by a click: pass `reconcile=` a callable that asks your business store
  what actually happened and returns `{"proven": True, "state": "succeeded"|"failed"}`.
  Without evidence the run stays suspended, which is the correct answer.
* **The delegated gateway stays authoritative.** If your existing gateway already owns
  the ledger, give the harness a backend with `ownership == "host"`: it is called and
  its receipt is recorded, and this package will not create a second intent or a
  second approval for the same call.

`DefaultHarness.state()` reads the *store*, so a host that serves several tenants
should pass `context=` and get an identity check for free. `EventEnvelope` is what a
`run_listener` receives (namespace, run, attempt, sequence, schema, payload); a
consumer that failed is repaired with `replay_run_events(run_id, after=cursor)`.

## 1. Persistence

```python
class MyStore:                      # implements yanxu_harness.storage.Persistence
    durability -> Durability        # DURABLE only if a restart really keeps the data
    load_run / save_run             # compare-and-set: expected_version is mandatory
    claim_lease / release_lease     # a monotonic fencing token per resource
    append_events / read_events     # sequences never repeat; read_after is a cursor
```

* **CAS is the correctness mechanism, not a lock.** A shared in-process lock is
  an optimisation; the version check is what stops a late writer.
* **Never recycle a fencing token.** Releasing a lease must not reset the
  counter, or a stale worker holding fence *n* becomes valid again. (The SQLite
  reference implementation originally deleted the row on release and handed fence
  1 out twice; the shared contract check now fails that.)
* **Events are published after the commit.** If the fanout fails, replay from a
  cursor — never roll back a fact that already happened.
* **`durability` is a promise.** If your store is a dictionary, return
  `VOLATILE`, and do not use it for a restart example.

## 2. ApprovalStore

```python
get(key) / put(approval) / supersede(binding) / decide(key, status, decided_by, note)
```

* `put` is for a call that has no record. `supersede` is for the same call whose
  bound values moved — keep the older record as history.
* `decide` is idempotent for the same status and must refuse to overwrite a
  different decision.
* Never let a client submission write here directly: a submission expresses a
  decision, the store records the fact.

## 3. ToolExecutionBackend

Own exactly one of two modes:

| `ownership` | Who records intent and receipt | When |
| --- | --- | --- |
| `"harness"` | this package (see `SqliteExecutionBackend`) | standalone use |
| `"host"` | your gateway; the harness calls it and stores the receipt it returns | migrating an existing application |

Do not nest them. A host that already owns a ledger must not let the harness
create a second intent or a second approval for the same call — that is the
double-ledger failure the plan forbids. `begin()` returning an existing receipt
is how a replay is detected; keep that behaviour.

**The strong optional contract.** `begin()` cannot tell "no intent exists" from "the intent is
open" in a way a recovery can rely on, and it offers no read. A backend that wants the
automatic recovery guarantee (and a report that can name an operation's state) implements
`RecoverableLedger` as well:

| Method | Must do | Must not do |
| --- | --- | --- |
| `observe(intent)` | return `ABSENT` / `OPEN` / `RECEIPT` / `UNAVAILABLE` for the exact binding (namespace, session, run, model action, tool-call id, argument hash) | write anything; treat an unreadable ledger as absence |
| `admit(intent)` | in **one** transaction, record-or-read the intent and answer `ADMITTED` / `EXISTING_OPEN` / `EXISTING_RECEIPT` / `BINDING_CONFLICT` | run a handler; grant admission twice for one operation |

`SqliteExecutionBackend` implements both, with `BEGIN IMMEDIATE` around the admission, an
idempotent `settle` that refuses to overwrite, and `promote(receipt, evidence=…)` that only
moves an *unproven* row (open or unknown) to `succeeded`/`failed`, re-checks the binding and
requires evidence that names its source. A backend without the capability keeps working on the
normal path; the harness then reports `unsupported` instead of assuming nothing happened, and
`DefaultHarness.durability`/`gateway.ledger_recoverable` say which situation a deployment is in.

`ToolReceipt.handler_entered` is dispatch-phase evidence used by typed tool budgets. The SQLite
backend records it when `execute` crosses into the handler. The Gateway treats a receipt returned
by `execute` with no phase as entered (the backend execution boundary has completed); a backend
that can prove it stopped before the handler must return `handler_entered=False`. A receipt with
`handler_entered=None` outside that boundary is not evidence for a refund. In all cases, the
receipt state by itself does not prove that the handler did not run.

**Read-only opening.** Every store this package ships can be opened read-only
(`SqlitePersistence(path, read_only=True)`, `SqliteExecutionBackend(path, read_only=True)`).
A read-only connection refuses every write path, does not create the file, does not set the
journal mode and does not migrate — but it still *checks* the schema version, so a database
written by a newer build is refused rather than misread. That is how the diagnostics reader
reads a host's stores, and a custom backend should offer the same property if it wants to be
inspectable.

## 4. Provider

```python
stream(request, cancellation) -> AsyncIterator[ProviderEvent]
```

`yanxu_harness.providers.OpenAICompatibleProvider` is a working adapter you can
use or copy. If you write your own:

* credentials reach you through `ProviderConfig.api_key`/`headers` only — never
  log them, never put them in a payload, and redact error bodies before they
  become messages;
* map HTTP/provider failures onto `ProviderFailureKind`; do not collapse them;
* put signatures and opaque blobs in `ProviderEvent.private`; `public_payload()`
  is what may be published;
* report usage as-is: a missing number stays `None`, it does not become `0`;
* do not retry. A retry is a decision the caller makes with knowledge of the
  effect state.

## 5. SkillProvider

Implement `list_metadata`, `load_body`, `read_resource`, `fingerprint`. Enforce
`SkillLimits` and confine resource paths (`safe_resource_path` plus a realpath
check). `InMemorySkillProvider` and `LocalSkillProvider` are the two reference
implementations; a host that reads skills from a database, an archive or a
signed bundle should reuse their semantics rather than their code.

## 6. DecisionRouter

Optional. `NoopDecisionRouter` means "no decision model configured", and every
task still runs. If you supply a model call to `ModelDecisionRouter`, the package
handles the budget, the cache, the validation and the failure states; you only
have to make the call and return `{"suggestions": [...]}`.

A router never grants anything. If your adapter ever needs to *authorize* a tool,
it is an authorization policy, not a decision router.

## 7. Managed context (optional)

With no `context_controller` the transcript *is* the request view: nothing is measured, nothing
is projected and no auxiliary model is ever called. Everything in this section is therefore an
explicit host decision, and each part is a separate input.

```python
from yanxu_harness.context import (
    ContextController, ContextWindow, HeuristicTokenEstimator, ToolOutputPolicy,
)
from yanxu_harness.storage import SqliteArtifactStore

artifacts = SqliteArtifactStore("artifacts.db")      # durable originals behind references

controller = ContextController.compose(
    estimator=HeuristicTokenEstimator(),             # or your own TokenEstimator
    converter=measurement_input,                     # the *same* conversion the wire uses
    window=ContextWindow(limit_tokens=200_000, output_reserve=8_000, safety_margin=2_000),
    max_payload_bytes=1_000_000,                     # an independent byte limit
    output_policy=ToolOutputPolicy(max_chars=4_000, require_durable=True),
    artifacts=artifacts,
    summarizer=my_summarizer,                        # optional: omit it and no aux call happens
    summary_budget=SummaryBudget(max_calls=1),
)
harness = DefaultHarness(stream_fn=bridge, tools=tools, persistence=store,
                         context_controller=controller)
```

| You supply | Where it goes | Why it must come from you |
| --- | --- | --- |
| the model window | `ContextWindow(limit_tokens, output_reserve, safety_margin)` | the window and the reserved output are deployment facts; `limit_tokens=None` means unknown and is reported as unknown, never as a percentage |
| the measurement of a request | `estimator=` (+ `converter=`) | the estimator is a port (`TokenEstimator.measure(MeasurementInput)`); the default counts characters and says so. `converter` must be the conversion the wire request is built from, or the measured request and the sent one are different documents |
| where originals live | `artifacts=` | a bounded tool-output view refers to stored bytes; see the durability rule below |
| how much of one result may be shown | `output_policy=ToolOutputPolicy(...)` | how big is "too big" is a deployment decision; with `require_durable=True` a volatile store is refused rather than referred to |
| an optional summarizer | `summarizer=`, `summary_budget=` | calling a model to shrink the prompt has a cost and a data-flow consequence; no default is shipped |
| the task view, if you keep one | `context/notes.py`'s `NoteLedger` (a host-mandated message) | only you know which statements are yours, the user's, or the model's |
| reading an original back | `tools/artifact_read.py`'s `artifact_read_definition(store)` in your registry | it is a READ-effect tool, so your policy decides who may read |

Four rules that are easy to get wrong:

* **The scope is supplied by the harness, not by you.** `prepare(payload, *, scope=…)` receives
  `namespace/session/run`; the controller isolates its projections, measurements, caches and
  summary calls by it. A cache key that cannot express a scope boundary is a bug.
* **A projection never replaces the conversation.** The prepared view and its facts live beside
  the transcript (`ExecutionState.request_view` / `context_state`, committed in the same
  transaction as the state they describe). `RunRecord.messages` keeps every message, so a
  later turn and a reader can always see what was there. `context_state` is sticky until
  replaced, so a settled batch does not erase the description of the request that was sent.
* **A reference must be resolvable.** `bounded_tool_output` stores the original *before* the
  reference exists, and a store that cannot hold it is a refusal, never a dangling id. Give
  `artifacts=` a store whose `durability` is `DURABLE` (`SqliteArtifactStore`) if your
  references have to survive a restart, and say so with
  `ToolOutputPolicy(require_durable=True)`: a volatile store is then refused up front instead
  of leaving an id nothing can resolve.
* **Do not combine the managed controller with the legacy in-place transforms.** Supplying
  `prepare_context` together with `transform_context` or `persist_transformed_context=True`
  is refused before dispatch (`validate_context_configuration`), because two different
  transformations over one request would make the measured request and the sent one differ.
  `convert_to_llm` is fine: it runs once, over the view preparation produced — give the
  controller the same conversion so the measurement and the wire agree.

Refusals are results, not exceptions: a request that cannot be made to fit ends the run
`usage_limited` with `error_code`/`stop_code` `context_budget_exceeded`, spends no turn and
dispatches nothing (the same holds for a refusal from the final payload gate). Nothing sends a
request the host policy did not approve of, and nothing truncates a tool argument.

<a id="plan-step-materials-c0-c2"></a><a id="plan-step-materials-c0-c3"></a>

### Plan Step materials (C0–C3)

The C0 public types describe bounded, sourced material for a Plan Step. C1 adds an optional
assembler that reads current Plan control facts and asks a host-owned resolver for only the
explicitly declared sources. C2 connects a completed manifest to the Provider request. The
host supplies the scope and owns source authorization:

```python
from yanxu_harness.context import (
    StepMaterialKind, StepMaterialRef, StepMaterialSource, StepMaterialTrust,
)

goal = StepMaterialRef.from_content(
    namespace="acme",
    scope="project:42",
    source=StepMaterialSource.APPROVED_PLAN,
    kind=StepMaterialKind.STEP_GOAL,
    source_id="plan:revision-7:step:review",
    source_version="7",
    authorization_ref="approval:plan-42:revision-7",
    content="Review the proposed change against the acceptance criteria.",
    required=True,
    trust=StepMaterialTrust.APPROVED_CONTROL,
)
```

Each `StepMaterialRef` binds namespace, scope, source, kind, source ID/version, SHA-256 digest,
authorization reference, validity and exactly one payload representation: inline text or an
`ArtifactRef`. Its manifest record contains provenance and the digest, never inline source text.
For an artifact, `byte_length` reports the original size while `included_bytes` measures only
the serialized reference included in this context. An artifact reference does not itself prove
that a source resolver authorized access; C1 checks source authorization and artifact presence
before publishing a manifest.

`StepContextManifest` binds the material list and an explicit kept/dropped reason for every
item to the Plan revision/definition digest, Step, execution binding, policy and estimator
versions, budget facts and rendered-context digest. Required material cannot be dropped, and
required or kept material must still be usable at `created_at`. Item and total limits are
checked without truncation; an impossible required set raises `StepContextRefused`, whose
record says network dispatch is disallowed. C2 maps this refusal to the actual Provider
dispatch gate.

#### C1 resolver and consistency boundary

`StepContextAssembler` takes the Plan control reader, an optional host resolver, and optionally
an artifact store that implements `holds(artifact_id)`. It accepts one explicit `namespace`,
`scope`, Plan, Step and current binding. It requires the Plan to remain in `EXECUTING` with its
current approved, unexpired, unrevoked revision, and requires the requested binding to be the
latest admitted or active binding for that Step. Required Plan resources become required reads;
Step `input_refs` are optional. A Plan overview is omitted unless the caller asks for it.

The resolver implements `resolve(request)` and `recheck(request, resolution)`. Each request
contains the exact source kind, identity, version, namespace, scope, requiredness and opaque
reference. The resolver must enforce the caller's actual read permission at both calls, bound
parsing by item and byte limits before returning content, and return matching source digest,
version and authorization evidence. The assembler rejects identity, scope, authorization or
requiredness changes. A required source that is missing, stale, expired, oversized, or changed
during recheck refuses the whole assembly. An optional source that is unavailable, stale, or
changed is omitted with a reason. The resolver must be cancellation-cooperative; `asyncio`
timeouts cannot stop synchronous work already blocking its event loop or an unmanaged thread.

Accepted predecessor material is opt-in by direct dependency. The assembler uses the latest
binding in the current revision and requires all current acceptance criteria on that binding
to be accepted against one source snapshot. Only its explicit evidence references are resolved;
an old retry's acceptance or an acceptance without evidence is not reused as output. Artifact
references must exist in the configured store.

Each Plan/source read has a timeout and the whole assembly has a configurable total deadline
(default 120 seconds, maximum 300 seconds); source requests are capped at 128 by default and
at most 512. `StepContextLimits` separately caps item count, per-item bytes and total bytes.
These are fail-closed limits: required material is never truncated or silently dropped.

Before publication the assembler rechecks every resolved source and rereads Plan, binding and
selected acceptance facts. These stores do not share a transaction, so manifest schema v2
records `source_boundary.consistency = "bracketed_non_atomic"`, the Plan control version,
source request/resolution counts, an opaque source-vector digest, selected acceptance IDs and
omissions. This is evidence of bracketed reads, not a claim of a globally atomic snapshot.
The manifest does not authorize a Provider request. C2 persists its association before dispatch
and sends nothing if that persistence fails.

`APPROVED_CONTROL` is valid only for an approved Plan source. An accepted output must identify
its acceptance source and remains evidence data; `accepted_predecessor_refs` may name only
kept accepted-output materials. Model-originated material must be `MODEL_INFERENCE`, unverified,
and optional. `valid_at()` excludes unverified material; `usable_at()` can include an unverified
suggestion as visibly unverified data, but neither the suggestion nor an accepted output can
grant a tool or become a trusted system instruction.

#### C2: Provider request and recovery wiring

`StepContextRuntime` is the opt-in adapter between the current Plan binding, the C1 assembler,
and the existing `ContextController`. Its `plan_store` must expose
`read_plan_binding_for_task(task_id)` in addition to the Plan and acceptance reads used by the
assembler. `DefaultHarness` still requires `task_id`; the Plan reader is independent of the Task
inbox/control store. The default selector chooses no predecessor output and no Plan overview:
the current approved Step goal, required constraints and acceptance criteria are assembled by
the C1 assembler, while previous outputs and extra sources require an explicit host selection.

```python
from yanxu_harness.context import (
    StepContextAssembler, StepContextRuntime, StepContextSelection,
)

plan_reader = host_plan_store  # Plan reads + read_plan_binding_for_task(task_id)
assembler = StepContextAssembler(plan_reader, resolver=host_material_resolver)
step_materials = StepContextRuntime(
    assembler=assembler,
    plan_store=plan_reader,
    select=lambda binding, payload: StepContextSelection(
        predecessor_steps=("read_sources",),
        required_predecessor_steps=("read_sources",),
    ),
    scope_for=host_authorized_material_scope,
)
harness = DefaultHarness(
    stream_fn=bridge,
    context_controller=controller,
    step_context=step_materials,
    task_id=task_id,
)
```

The callback names above stand for host functions. The selector must name only direct
predecessors declared by the current Plan; the resolver still checks authorization on each
read and recheck. `StepContextRuntime` renders each material into a `user`-role message with a
reference-data notice, then asks the existing controller to project the augmented request. It
passes required material messages as mandatory, so the controller either keeps them or returns
a refusal. Optional material may be omitted under the rendered-byte limit or the complete
request budget; the manifest records `render_byte_limit` or `request_context_budget` for the
drop.

The controller's configured converter remains the measurement boundary. It must match the
Provider bridge conversion so the measured system prompt, active tool declarations and messages
are the ones the transport will send. The request output cap is read from the current model and
the configured output reserve is deducted by `RequestBudget`; the final serialized body is still
checked by the Provider payload gate. `max_render_bytes` limits only the serialized material
messages. It does not prove that the complete request fits the token window.

The material messages are derived request input, not a transcript rewrite. They are inserted
immediately before a final user prompt when one is present; after a completed transcript
round-trip otherwise, so older turns remain eligible for projection and a tool call/result pair
is never split. The summary compactor keeps host-mandated material messages verbatim even when
they fall outside its ordinary recent-turn tail. Material never becomes system instructions or
tool/approval policy. `RunRecord.messages` remains the original conversation.
The request view and `context_state.step_context` manifest association are committed through the
existing `RequestPrepared` state sink before `SendModelRequest`; a failed state commit produces
zero Provider calls. The persisted manifest record carries its digest, decision reasons and the
digest of the exact rendered material messages, but not inline material content.

On resume or the next turn, the runtime reads the current Plan binding and assembles a fresh
manifest from current source versions. The existing controller receives the prior committed
context facts, including summary allowance and usage, so recomputation does not refresh an
auxiliary-model budget. A revoked approval, changed Plan revision, or inactive binding refuses
assembly. A change to execution authorization or approved input semantics is handled by the
host's Plan revision/approval flow; refreshing a manifest cannot grant that authority. Artifact
materials are sent as references only. A host that needs the model to read original bytes must
also expose an appropriately scoped READ-effect artifact reader through its tool registry.

#### C3: three-Step example

[`examples/20_step_context.py`](../examples/20_step_context.py) connects Plan, Task and Run SQLite
stores, `LocalWorker`, `DefaultHarness`, the context runtime and a host-owned resolver. The child
process exits after committing the first Run; the parent reopens the same stores, settles and
accepts that Run, then proceeds through candidate generation and an independent host verifier.
The verifier's actual Provider request is checked against the committed manifest digest.

The third Step receives only its declared current source plus the direct accepted candidate.
The example does not reuse the earlier accepted Step output or infer authorization from model
text. Tests also refuse a changed source, missing artifact, revoked permission, oversized output,
cross-scope resolution and parser failure before Provider dispatch. The complete boundary and
coverage list is in the [implementation progress](harness-next-stage-progress.zh-CN.md).

## 8. Optional integrations: the MCP client

`yanxu_harness.integrations.mcp` is an **optional** adapter (`pip install 'yanxu-harness[mcp]'`)
that exposes remote MCP tools as ordinary `ToolDefinition`s, so they run through the same
gateway, approvals and single operation ledger as local tools. Nothing in the base
package imports it, and the SDK it needs is imported lazily.

A host supplies the MCP-specific half explicitly, and none of it can come from the
server:

```python
config = McpServerConfig(server_id="notes", transport=McpTransport.STDIO,
                         protocol="2025-11-25", authorization_scope="notes-v3",
                         command=sys.executable, args=("notes_mcp.py",))
bindings = {"notes_search": McpToolBinding("notes_search", effects=frozenset({ToolEffect.READ}))}
snapshot = await discover_catalog(manager, connection, config=config, bindings=bindings)
```

The catalog is not a permission: only tools named in `bindings` become entries, effects
and previews are the host's decision, and every definition is versioned so a recovered
run can refuse a changed tool instead of silently re-freezing it. See the
[MCP client guide](mcp-client.zh-CN.md) for the limits, failure states and recovery
shape.

## 8b. Composing tools, skills and MCP into one deployment (optional)

The extension layer is a **host-side assembler**, not another runtime. A host that wants one
explicitly bound capability pack builds a plan from data and host objects:

```python
plan = plan_extensions(
    manifests=[parse_manifest(document)],
    factories={"example.reference": host_factory},   # trusted host code
    bindings=[host_binding],                         # namespace/revisions/grants
    local_tools=registry.definitions(),
)
async with open_extensions(plan) as deployment:
    gateway = ToolGateway(registry=deployment.registry, policy=my_policy,
                          approvals=my_approval_store, backend=my_execution_backend)
    bindings = GatewayBindings(gateway=gateway, context=my_context)
    harness = deployment.bind_harness(
        stream_fn=my_stream, gateway_bindings=bindings, persistence=my_persistence,
    )
```

The helper publishes a read-only registry and a restricted `SkillProvider`; it does not
create a gateway, ledger, scheduler or prompt. The host still supplies policy, approvals and
the execution backend, and `deployment.registry` is the exact registry the gateway must
read. A revoked/closed deployment makes old tool/skill/harness references refuse before a
new intent; the same guard checks the host context's namespace and authorization scope, and
is re-run after an approval-store await before admission. A resume under a different
extension version or binding revision is refused by the existing arrangement comparison
because the deployment digest is folded into `config`; a host-supplied `config` value is an
input to that digest, not an override.

The optional MCP pack uses the same adapter and the same ledger:

```python
factory = mcp_tool_factory(config=mcp_config, bindings=mcp_bindings)
plan = plan_extensions(manifests=[manifest], factories={"...": factory},
                       bindings=[binding])
```

There is no second MCP implementation and no second operation ledger. A borrowed manager,
adapter, connection or snapshot must declare its namespace and match the deployment's
actual authorization scope/config revision; a declared manager must be the adapter's
actual manager, and an unverifiable borrow is refused before contribution. The API,
limits, lifecycle and failure rules are in the [extension guide](extensions.zh-CN.md); the
[E0–E3 delivery report](extensions-delivery-report.zh-CN.md) records the acceptance
evidence.

## Wiring it together

```python
gateway = ToolGateway(
    registry=registry,            # definitions (layer 1)
    visibility=my_visibility,     # deployment policy (layer 2)
    policy=ConservativePolicy(),  # approval policy
    approvals=my_approval_store,
    backend=my_execution_backend,
)
result = await gateway.invoke(invocation, run_context)
```

`gateway.invoke` is the only supported way to run a tool: it enforces the layer
order, records the intent, and returns a state (`executed`, `replayed`,
`awaiting_approval`, `denied`, `invalid_args`, `cancelled`, `unknown`) instead of
raising for the ones that are not errors. Use `result.raise_for_status()` when a
refusal really is exceptional.

When a typed dispatch budget is configured, the Gateway checks a revocable execution binding
again after the awaited reservation and immediately before execution. Model reservation adapters
receive the complete expected owner-to-policy-digest map and must compare it with current scopes
inside the same transaction or lock that writes the batch. An adapter that cannot perform this
check must fail the dispatch closed before calling its Provider.

## 9. Persistent tasks and parent/child work (optional)

A host that wants durable input, several queued tasks or a local subagent tree wires four
dependencies. Nothing here starts a thread or contacts a network on import.

```python
from yanxu_harness.tasks import (
    ChildCoordinator, ChildPolicy, LocalWorker, SqliteControlStore, require_coordination,
)
from yanxu_harness.runtime import DefaultHarness

control = SqliteControlStore("state/control.db")   # run store + control tables, one file
require_coordination(control)                       # refuse a partial backend before accepting work

coordinator = ChildCoordinator(
    control,
    policy=ChildPolicy(
        profiles=frozenset({"researcher"}),
        tools=frozenset({"search"}),
        workspace_root="/work",
        budget_units={"model_requests": 5.0},
    ),
)

def harness_for(task, attempt_id):
    return DefaultHarness(
        stream_fn=my_stream,                 # the provider bridge, unchanged
        tools=[...],
        persistence=control,                 # the run store
        control=control,                     # the inbox and the atomic commit
        task_id=task.task_id,
        finish_barrier=coordinator.finish_interaction,  # pause before a parent's terminal commit
    )

worker = LocalWorker(control, harness_for)
await worker.submit(my_task_admission)     # idempotent by task id
await worker.run_until_idle()              # explicit; no daemon
```

What the host owns and what the library owns:

* **the host decides the policy.** `ChildPolicy` bounds profiles, tools, workspace and grant;
  the coordinator intersects it with the parent profile's delegated scope. A prompt cannot widen
  it, and a child cannot be granted a tool its parent's scope excludes.
* **the library schedules, it does not run models.** `LocalWorker` picks, claims and resumes a
  task; the model request, the tool batch, the commit boundary and the recovery remain the one
  `Runner` inside `DefaultHarness`.
* **one Task, one Run.** A completed task is not revived: a new user request is a new
  `TaskAdmission`. `waiting` releases the worker slot; the host persists the awaited fact (an
  approval in the approval store, a child's committed result) and calls `wake`, or lets the
  coordinator's `deliver_notifications` wake it.
* **the host supplies the workspace and the result verifier.** A `ResultVerifier` decides business
  acceptance (schema validity is only a format check); workspace merges stay on the host's
  authorisation path.
* **a backend that cannot commit atomically is refused, not degraded.** `require_coordination`
  raises before the first admission; a single-run deployment simply does not enable this layer.

## 10. Optional Plans and DAG orchestration

Plans add a host-approved dependency graph over persistent Tasks. They are an opt-in layer on the
same control store and the same `DefaultHarness`/Core Runner/ToolGateway path. A Plan approval
does not grant tool permission, and a Task reaching `completed` does not by itself satisfy a step's
acceptance criteria.

```python
from yanxu_harness.tasks import LocalWorker, SqliteControlStore

control = SqliteControlStore("state/control.db", enable_orchestration=True)
worker = LocalWorker(
    control,
    harness_for,  # builds the regular DefaultHarness with this store and Task ID
    enable_orchestration=True,
    orchestration_namespace="project-a",
)
```

The host still validates proposals, authenticates the approval principal, deploys the allowed
tools and Gateway policy, reads committed Task/Run/operation facts, and registers read-only
verifiers for each versioned acceptance criterion. It also chooses when to pause, retry or
replan, reconciles cancellation after process restart, and delivers durable outbox notices. An
unresolved or unreadable operation remains unknown and blocks acceptance and replan publication.
The Plan-aware worker shares its normal scheduling slot and execution path; it does not start a
daemon or add a second model loop.

For the complete offline wiring and cross-process recovery example, see
[`examples/15_plan_dag.py`](../examples/15_plan_dag.py) and
[`examples/16_plan_recovery.py`](../examples/16_plan_recovery.py). The host responsibilities,
migration levels and read-only `PlanInspector` command are detailed in
[`plans-integration.zh-CN.md`](plans-integration.zh-CN.md); frozen semantics are in
[`plans-contract.zh-CN.md`](plans-contract.zh-CN.md).
