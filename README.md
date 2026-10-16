# Yanxu Harness

English | [简体中文](README.zh-CN.md)

**An embeddable Python agent runtime with durable execution, tool approvals, and recovery backed by operation evidence.**

Build agents inside your application while keeping control of permissions, data, and business operations. Yanxu tracks execution progress, settles tool calls individually, and separates the full conversation from the context sent to the model. The base package has **zero third-party runtime dependencies**.

> **Alpha · Python 3.10+ · MIT.** The current source version is `0.1.0a2`; no package-index release is published. Install from source. APIs and persisted formats may change; see the [public contract](docs/contract.md) and [verification record](docs/verification.md).

## Why Yanxu?

An agent can stop after a tool has changed your data but before its result has been saved. Continuing that run requires knowing what happened outside the model conversation.

Yanxu makes those execution boundaries explicit:

- **Recover with evidence.** Reuse confirmed tool results. Pause for reconciliation when an operation's outcome cannot be proven.
- **Keep business authority in your application.** The host establishes identity, grants permissions, records approvals, and supplies tools. Model output and skill text cannot grant authority.
- **Understand each run.** Inspect persisted progress, waiting reasons, tool evidence, context decisions, and usage through a read-only API or CLI.
- **Embed in an existing Python stack.** Inject your provider, tools, persistence, and policy through explicit interfaces. SQLite reference implementations are included.

Useful for application agents that edit business data, wait for approval, or need to continue after a process restart. Bring your own tools and workspace when building a coding agent.

## Quickstart

### Install from source

```bash
git clone https://github.com/SZWzz/yanxu-harness.git
cd yanxu-harness
python -m venv .venv
source .venv/bin/activate
python -m pip install .
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. Optional adapters: `python -m pip install '.[yaml,pydantic]'` adds local `SKILL.md` front-matter parsing and Pydantic tool argument models.

### See recovery work — no API key needed

```bash
python examples/01_basic_dialogue.py
python examples/07_partial_batch_recovery.py
```

The recovery example starts a real child process, completes tool A, crashes inside tool B, and leaves tool C unstarted. A fresh harness reads the saved state and operation ledger. It waits for evidence about B, then finishes C after a host reconciler supplies that evidence.

Output, abridged:

```text
step: crash
  A settled and was committed; B is open; C never ran
step: recover-barrier
  resumed, then stopped: B is unproven, so A was not re-run and C was not started
step: evidence
step: finish
  A replayed, B proven by evidence, C run exactly once; the model saw all three
OK: a partially settled batch was recovered call by call
```

The examples use explicitly scripted model responses. The harness, tools, SQLite stores, process failure, and recovery are real; this demonstrates execution behavior, not model quality. Temporary state is cleaned up after the example.

### Connect your model

Set `YANXU_BASE_URL` to your OpenAI-compatible endpoint's base URL, `YANXU_MODEL` to its model name, and `YANXU_API_KEY` to your credential in your application's environment. This example makes a real network request:

```python
import asyncio
import os
import uuid

from yanxu_harness.providers import (
    CoreStreamBridge,
    OpenAICompatibleProvider,
    ProviderConfig,
    SecretReference,
)
from yanxu_harness.runtime import (
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
)
from yanxu_harness.storage import SqlitePersistence


async def main():
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            model=os.environ["YANXU_MODEL"],
            base_url=os.environ["YANXU_BASE_URL"],
            api_key=SecretReference("model-key"),
        ),
        secrets={"model-key": os.environ["YANXU_API_KEY"]},
    )
    store = SqlitePersistence("runs.db")
    try:
        harness = DefaultHarness(
            stream_fn=CoreStreamBridge(provider),
            persistence=store,
            system_prompt="Be concise and explain the basis for your answer.",
            max_runtime_seconds=60,
        )
        context = RunContext(
            identity=RunIdentity(
                namespace="demo", session_id="s1",
                run_id=uuid.uuid4().hex, attempt_id="a1",
            ),
            principal=Principal(subject="local-user"),
        )
        outcome = await harness.start(
            RunRequest(prompt="What makes an agent recoverable?", max_turns=3),
            context,
        )
        print(outcome.status.value, outcome.stop_code)
        print(outcome.final_text)
    finally:
        store.close()


asyncio.run(main())
```

This minimal run has no tools. Add tools through `ToolDefinition` → `ToolGateway` → `core_tool`; the [approved-edit example](examples/05_durable_approved_edit.py) shows the complete wiring. The [real-model edit example](examples/optional/real_model_edit.py) uses the same deployment with a configured endpoint.

## What is implemented

| Capability | What it gives your application |
| --- | --- |
| Explicit execution state | A pure state reducer and one Runner; versioned snapshots and commits before model/tool dispatch |
| Tool execution and recovery | Bounded concurrency, per-call settlement, stable operation identities, receipt reuse, and reconciliation barriers |
| Durable approvals | Preview and approval bindings tied to arguments, target versions, policy, and expiry; pause and resume across processes |
| Managed context | Separate transcript and request view, dependency-aware history selection, request budgets, output references, and optional bounded summaries |
| Provider integration | Neutral streaming protocol, OpenAI-compatible transport, cancellation, and host-supplied credentials |
| Skills and decision routing | On-demand skill metadata/body/resources; optional rule or model routers, within host permissions |
| Read-only diagnostics | JSON/Markdown reports with provenance, waiting reasons, tool evidence, stage timings, and usage coverage |
| Offline task evaluation | Frozen cases, independent verifiers, failure injection, and comparison of matching cases |

## How execution works

The core decides the next action; the Runner performs it and returns an input event. Durable execution through `DefaultHarness` commits progress before dispatching external work.

```mermaid
flowchart TD
    Host["Your Python application"] --> Runtime["DefaultHarness: start / resume / cancel / state"]
    Runtime --> Core["Pure reducer: state + event → transition"]
    Core --> Commit["Commit execution progress"]
    Commit --> Store[("Run state and transcript")]
    Commit --> Runner["Runner performs the next action"]
    Runner --> Context["Prepare context and request facts"]
    Context --> Core
    Runner --> Model["Model provider"]
    Runner --> Gateway["ToolGateway: policy, approval, operation evidence"]
    Gateway --> Tools["Host tools and business data"]
    Model --> Core
    Gateway --> Core
```

The full transcript, the model's request view, and operation receipts have distinct roles. The Gateway or delegated host owns operation evidence; a saved conversation or a model's claim of completion cannot substitute for it. See the [current architecture](docs/current-harness-architecture.zh-CN.md) and [recovery guide](docs/recovery.md).

## Pause, approve, and resume

Run the complete offline approval demo:

```bash
python examples/05_durable_approved_edit.py
```

To inspect a paused run yourself, choose a fresh state directory and start it separately:

```bash
python examples/05_durable_approved_edit.py --state build/approval-demo start --task "Update the welcome text"
python examples/05_durable_approved_edit.py --state build/approval-demo status --run edit-1
```

The output includes the preview and approval binding key. Use that key with the example's `approve` command, then its `resume` command; see [the example's instructions](examples/05_durable_approved_edit.py). Approval is a trusted host operation. Passing `{"approved": true}` to `resume()` does not authorize a write.

Run outcomes distinguish `completed`, `suspended`, `usage_limited`, `cancelled`, `failed`, and `rejected`. Recovery checks the stored configuration baseline; instructions, tools, or policy drift can refuse a resume before dispatch. See the [lifecycle contract](docs/contract.md).

## Inspect and evaluate

Inspect the saved approval demo without executing tools or resuming the run:

```bash
python -m yanxu_harness.diagnostics inspect \
  --runs build/approval-demo/runs.db \
  --operations build/approval-demo/operations.db \
  --namespace example-project --session session-1 --run-id edit-1 --format markdown
```

See a [diagnostic report](docs/samples/partial-recovery-report.md) for a partially recovered batch. Reports distinguish actual, estimated, and unknown measurements; missing usage is not counted as zero.

Run the frozen offline task suite:

```bash
python -m yanxu_harness.evals run --root build/evals --out build/eval-report
python -m yanxu_harness.evals --help
```

The suite exercises defect location, implementation fixes, diff review, approval, recovery, refusal, and context cancellation. Independent verifiers check results. These scripted-model cases measure harness behavior; they do not establish real-model task performance. See the [evaluation samples and comparison](docs/samples/evals/README.md).

## Scope and current limits

- **Alpha compatibility:** persisted formats are versioned. Unknown versions and older states that lack evidence required for safe recovery are refused.
- **External effects:** arbitrary tools and hooks do not get a blanket exactly-once guarantee. Reliable recovery needs durable receipts, idempotency, or host-provided business evidence; an uncertain outcome remains blocked.
- **Host integration:** the host owns authentication, permission grants, business transactions, and sandboxing. A custom operation ledger must implement the recovery capabilities the harness needs.
- **Roadmap:** durable inboxes, multi-task scheduling, subagent execution, and a general plugin system are not implemented. Follow the [evolution roadmap](docs/harness-evolution-roadmap.zh-CN.md).
- **Evidence:** model quality and platform support must be checked against the recorded version and environment. See [verification](docs/verification.md), [support](SUPPORT.md), and the [diagnostics/evaluation delivery report](docs/runtime-diagnostics-evaluation-delivery-report.zh-CN.md).

## Examples and documentation

| Start here | Purpose |
| --- | --- |
| [Basic dialogue](examples/01_basic_dialogue.py) | One model turn, streaming events, and a transcript |
| [Approved data edit](examples/02_approved_data_edit.py) | Tool definitions, policy, and approval |
| [Cross-process recovery](examples/03_cross_process_recovery.py) | Reopen persisted execution in another process |
| [Tools, skills, and decisions](examples/04_nonfiction_tools_skills_decisions.py) | Combine the optional layers |
| [Durable approved edit](examples/05_durable_approved_edit.py) | Preview → pause → approve → resume |
| [Managed-context edit](examples/06_managed_context_edit.py) | Run a coding task with a bounded request view |
| [Partial-batch recovery](examples/07_partial_batch_recovery.py) | Crash → reconcile → finish the remaining work |

For integration, read the [public API contract](docs/contract.md), [adapter guide](docs/adapters.md), and [recovery semantics](docs/recovery.md). Architecture and implementation plans are available in Chinese: [current architecture](docs/current-harness-architecture.zh-CN.md), [Core execution](docs/core-execution-current.zh-CN.md), and [roadmap](docs/harness-evolution-roadmap.zh-CN.md).

## Contributing

Integration feedback, small runnable failure cases, adapter improvements, and documentation fixes are useful contributions. Include expected behavior and the relevant environment; remove credentials and private data. For security reports, follow [SECURITY.md](SECURITY.md).

```bash
python -m pip install -e '.[dev,yaml,pydantic]'
python -m pytest -q tests
python scripts/ci_check.py --prepare-wheelhouse build-wheelhouse
python scripts/ci_check.py --offline --wheelhouse build-wheelhouse
```

The wheelhouse preparation step uses the network; the subsequent gate installs and verifies artifacts offline. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

## License and acknowledgements

[MIT](LICENSE). The core's message types, event vocabulary, and parts of its loop behavior derive from **pi-agent-core**. Yanxu adds an explicit execution state machine, one Runner, durable commit boundaries, and its application integration layers. Attribution and the upstream MIT license are retained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [LICENSES/](LICENSES/).
