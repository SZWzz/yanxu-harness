# yanxu-harness (working name)

An embeddable Python agent harness. One model loop, four-layer tools,
three-layer skills, optional decision routing, context budgets and durable
approval/recovery — with no application framework in the base dependency
closure.

> **Status: local candidate, not published.** `0.1.0a1` is a working version
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

Project documents:

* `SUPPORT.md` — supported Python versions, platforms and the dependency table
  (base install is standard library only; extras are per adapter).
* `CONTRIBUTING.md` — scope, setup, the gate to run, and the rules this package
  follows.
* `SECURITY.md` — the security model, and how to report an issue (no reporting
  channel is published yet — do not invent one).
* `CHANGELOG.md` — what `0.1.0a1` contains and what is deliberately absent.

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
