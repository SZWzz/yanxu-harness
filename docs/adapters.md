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

## 0. Wiring the harness: the five things a host supplies

The ports below are what an adapter implements. This section is what a host *passes
to* `DefaultHarness`, which is the smallest complete integration:

| You supply | Where it goes | Why it must come from you |
| --- | --- | --- |
| a provider bridge | `stream_fn=CoreStreamBridge(provider)` | the bridge is provider-neutral; the endpoint, model and credentials are yours |
| the model-visible tools | `tools=[core_tool(definition, bindings), …]` | a registered tool is not thereby visible; the deployment decides |
| a store | `persistence=SqlitePersistence(path)` (or your own `Persistence`) | durability is a property of your storage, not of this package |
| identity | `RunContext(identity=…, principal=…)` on every call | authority is established server-side and never derived from model output |
| (optional) policy seams | `stop_after_turn`, `suspend_after_batch`, `fingerprints`, `reconcile`, `run_listener` | stopping, approving and reconciling are host decisions |

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
usually wants to shortcut and must not:

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
