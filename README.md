# yanxu-harness (working name)

An embeddable Python agent harness. One model loop, four-layer tools,
three-layer skills, optional decision routing, context budgets and durable
approval/recovery — with no application framework in the base dependency
closure.

> **Status: local candidate, not published.** `0.1.0a2` is a working version
> string, not a released artifact. The source lives at
> <https://github.com/SZWzz/yanxu-harness>; nothing is published to any package
> index, there is no release account behind an upload and there is no tag, badge
> or package URL to link to. The distribution name `yanxu-harness` and the import
> name `yanxu_harness` have not had an availability check against an index, and
> the version string is the package's own.

## Design in one screen

```text
your application ──► yanxu_harness ──► neutral ports (stdlib only)
                        │
                        ├── core/        the only model loop, neutral messages, streaming, cancellation
                        ├── runtime/     run lifecycle, budgets, suspended/resumed runs, run context
                        ├── tools/       definition+registry, deployment visibility, discovery, execution
                        ├── skills/      metadata catalog, on-demand body/resources, session binding
                        ├── decisions/   no-op / rule / model decision routers (all optional)
                        ├── context/     transcript archive, model projection, summarization budget
                        ├── storage/     persistence/unit-of-work ports, in-memory and SQLite reference stores
                        └── providers/   provider protocol plus explicitly configured transports
```

Dependency direction is fixed: **application → harness → neutral ports /
standard library / explicitly chosen extras**. The harness never imports an
application package, a web framework, an application store, a front end or a
Node.js toolchain. A missing optional extra can only break the adapter that
asked for it.

## Base install

The base package needs no third-party runtime dependency:

```bash
python -m venv .venv
. .venv/bin/activate
pip install yanxu_harness            # when a release exists
# or, from a local checkout:
pip install .
```

Optional extras are per adapter: `[pydantic]` for argument models,
`[yaml]` for `SKILL.md` front matter, `[dev]` for tests and building.

## Install

The base package needs nothing outside the standard library, so an install is one
command and an optional adapter is one extra:

```bash
python -m venv .venv && . .venv/bin/activate
pip install yanxu_harness              # when a release exists
pip install 'yanxu_harness[yaml]'      # + SKILL.md front matter parsing
pip install 'yanxu_harness[pydantic]'  # + argument models for tool definitions
# or, from a source tree or a built artifact:
pip install .            # a checkout
pip install dist/yanxu_harness-0.1.0a2-py3-none-any.whl
pip install dist/yanxu_harness-0.1.0a2.tar.gz
```

Everything below assumes the package is importable from wherever you installed it;
no example reaches back into a source tree.

## Run a task, then find out what happened

```python
import asyncio
from yanxu_harness.providers import CoreStreamBridge, OpenAICompatibleProvider, ProviderConfig, SecretReference
from yanxu_harness.runtime import DefaultHarness, Principal, RunContext, RunIdentity, RunRequest
from yanxu_harness.storage import SqlitePersistence

provider = OpenAICompatibleProvider(
    ProviderConfig(model="your-model", base_url="https://your-endpoint/v1", api_key=SecretReference("YOUR_KEY")),
    secrets={"YOUR_KEY": your_host_resolves_this()},
)
harness = DefaultHarness(
    stream_fn=CoreStreamBridge(provider),
    tools=[...],                       # core ToolSpecs; see docs/adapters.md
    persistence=SqlitePersistence("runs.db"),
    system_prompt="...",
)
context = RunContext(
    identity=RunIdentity(namespace="project", session_id="s1", run_id="r1", attempt_id="a1"),
    principal=Principal(subject="operator"),   # server-side authority, never model output
)

outcome = asyncio.run(harness.start(RunRequest(prompt="do the thing", max_turns=6), context))
print(outcome.status, outcome.stop_code, outcome.turns_used)
```

`outcome.status` is one of `completed`, `failed`, `cancelled`, `suspended`,
`usage_limited`, `rejected` — never `running`, because an active run has no outcome
and `state()` answers while it is alive:

```python
state = asyncio.run(harness.state("r1"))
if state.status is RunStatus.SUSPENDED:
    print(state.pending_interaction)      # what it is waiting for, and why
asyncio.run(harness.cancel("r1", "the operator stopped it"))
```

What the statuses mean in practice:

| Status | Meaning | What to do next |
| --- | --- | --- |
| `completed` | the model finished, or a host stop hook stopped the loop (`stop_code` tells them apart) | read `final_text` |
| `usage_limited` | the turn or runtime budget ran out; the last authorised batch was settled first | raise the budget and start a new run, or resume with a larger one |
| `suspended` | waiting for an approval decision or for evidence about an unproven operation | see the approve/resume example below |
| `cancelled` | the host cancelled it (from any thread); the partial transcript is kept | inspect receipts before deciding anything |
| `failed` | the model request failed; `error_code` is the provider's own kind (`rate_limited`, `context_overflow`, …) | decide, then start a new run |
| `rejected` | the run never started, e.g. an invalid budget (`invalid_max_turns`) | fix the request |

Budgets and limits, each explicit and refused rather than silently reinterpreted:
`max_turns` (`None` or a positive int; `0`, negative numbers and booleans are
refused before any request), `max_runtime_seconds` per invocation out of the
constructor, `lease_ttl_seconds` for the run's lease, and the context/payload
budgets in `docs/adapters.md`. `unknown` is not a status: an unprovable external
effect is reported by the tool layer and suspends the run.

## Approve, then resume in another process

```python
# 1. the run pauses before the write; the batch and the question are on disk
outcome = asyncio.run(harness.start(request, context))
binding = outcome.pending_interaction["approval_binding_key"]
print(outcome.pending_interaction["preview"])     # the real current value and the proposed one

# 2. the trusted decision path writes the decision (a local operator, a web handler you
#    authorised — this package does not authenticate users)
from yanxu_harness.storage import SqliteApprovalStore
from yanxu_harness.tools import ApprovalStatus
store = SqliteApprovalStore("approvals.db")
asyncio.run(store.decide(binding, ApprovalStatus.APPROVED, decided_by="operator"))

# 3. a *new* process — new harness instance, same files — resumes the run
new_context = RunContext(
    identity=RunIdentity(namespace="project", session_id="s1", run_id="r1", attempt_id="a2"),
    principal=Principal(subject="operator"),
)
outcome = asyncio.run(harness.resume("r1", {}, new_context))
```

A resume never re-runs something that may already have happened: a settled call's
recorded result is reused, a waiting call is re-dispatched under the *same*
operation id (so the gateway replays its receipt), and an unproven operation stays
`unknown` until a host-supplied reconciler returns business evidence. `resume`
refuses a run whose tool set, policy, instructions or visible-tool manifest drifted,
naming the drift; an expired approval goes back to the author.

## The whole chain, in one runnable example

`examples/05_durable_approved_edit.py` drives the end-to-end case — task → model
tool call → real preview → pause → process exit → new process reads the status →
operator approves → new process resumes → only the unfinished call runs → the model
answers. With no arguments it performs all of that in real child processes in a
temporary directory:

```bash
python examples/05_durable_approved_edit.py
```

To operate it by hand, the same four steps are separate commands (`--state DIR`
holds the run, operation and approval ledgers):

```bash
python examples/05_durable_approved_edit.py --state /tmp/edit start --task "更新欢迎语"
python examples/05_durable_approved_edit.py --state /tmp/edit status --run edit-1
python examples/05_durable_approved_edit.py --state /tmp/edit approve --approval <binding_key> --actor you
python examples/05_durable_approved_edit.py --state /tmp/edit resume --run edit-1
```

`approve` is a trusted local operator entry, not a web endpoint: this package does
not authenticate users, and a host that exposes approvals over HTTP must authorise
the request itself. The model in this example is a deterministic `Provider` double,
labelled as such; `examples/optional/real_model_edit.py` runs the same chain against
a real OpenAI-compatible endpoint when a host supplies one, and is deliberately
outside the automated gate.

## Managed context: the smallest coding-agent trial

`examples/06_managed_context_edit.py` is the first-delivery trial of the managed context
path (C0–C3 of the context plan). It runs a three-turn coding task — read `main.py`, write
a fix, answer — with a `ContextController` measuring every request against the model's
window:

```bash
examples/06_managed_context_edit.py
```

Its window is deliberately small, so the third request no longer fits the whole
transcript: the controller drops the oldest complete fragments, replays the effective
instructions and tool set into a baseline, and the harness commits the resulting view and
its facts *before* the provider is called. The example asserts what that means — the
transcript keeps all seven messages while the view sent is smaller, no view ever carries a
`toolResult` whose call was dropped, the committed facts describe the request that was
sent, and the model's answer echoes the bytes the tool really wrote — and exits non-zero
when one of those fails. Like the other examples it is offline and uses a labelled
`Provider` double; the measuring, projecting, committing and tool execution are real.

## Guarantees this package is built to keep

1. **One loop.** Exactly one model loop exists in the dependency closure.
   Compatibility shims re-export the same functions, types and exceptions, so
   `isinstance` checks and `except` clauses cannot split between two copies.
2. **Author-controlled writes.** Generated content stays staged until the
   application authorizes it. Approval is bound to the namespace, the call, the
   complete argument hash, the target version, the tool contract version, the
   policy version and an expiry — any drift invalidates it.
3. **Explicit unknown.** External side effects that cannot be proven are
   reported as `unknown`, never upgraded to success and never retried
   automatically.
4. **Skills are untrusted guidance.** Skill text, allowed-tool lists and bundled
   resources grant nothing: no permission, no approval, no script execution.
5. **Versioned state.** Events, checkpoints and schemas carry versions and
   fingerprints; a future version or an unprovable older state is refused
   instead of guessed.
6. **Raw history stays raw.** The archived transcript is not rewritten by
   summarization; the model projection is a separate, budgeted view.

## Documentation

* `docs/contract.md` — the public API, the stability levels and every behavioural
  guarantee, each pointing at the module that implements it.
* `docs/adapters.md` — what each of the six ports must do, and the shared checks
  you can run against your own implementation.
* `docs/recovery.md` — the failure states, the four crash windows, lease fencing
  and what this package deliberately does not promise.
* `docs/verification.md` — what is verified, by which test, from which kind of
  evidence, and what is deliberately *not* verified.

Project documents:

* `SUPPORT.md` — supported Python versions, platforms and the dependency table
  (base install is standard library only; extras are per adapter).
* `CONTRIBUTING.md` — scope, setup, the gate to run, and the rules this package
  follows.
* `SECURITY.md` — the security model, and how to report an issue (no reporting
  channel is published yet — do not invent one).
* `CHANGELOG.md` — what `0.1.0a2` contains and what is deliberately absent.

## Development

```bash
python -m pytest -q tests                     # this package's own suite
python -m build                               # wheel + sdist
python scripts/ci_check.py --prepare-wheelhouse build-wheelhouse   # network
python scripts/ci_check.py --offline --wheelhouse build-wheelhouse # the gate
```

The package's own tests import nothing from another repository; a host that
embeds the harness never imports these test helpers.

## License

MIT. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
