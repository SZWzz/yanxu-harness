# Contributing

> Status: **local candidate, not published.** There is no remote repository or
> contributor process yet. This file records how this package is actually built
> and checked, so the first outside contributor is not guessing.

## Scope: a harness, not an application

This package is deliberately generic. Changes belong here when they are about
*mechanism*:

* the single model loop, neutral messages, streaming, cancellation;
* tool definition/registry, deployment visibility, discovery, approval and
  execution;
* skills metadata/body/resources and session binding;
* optional decision routing (no-op / rule / model);
* the transcript archive, model projection and context budgets;
* persistence/unit-of-work ports plus the in-memory and SQLite reference stores;
* provider protocol and explicitly configured transports.

Changes do **not** belong here when they are application policy: brand names,
endpoints, model catalogs, story-specific profiles, which tools are visible for
a given product, per-product approval policy or HTTP/SSE/DTO shapes. Those live
in the host that embeds the harness. The harness must never import an
application package, a web framework, a front end or a Node.js toolchain — the
import fence is a test, not a convention.

## Set up

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev,yaml,pydantic]"
```

## The gate you must run

```bash
python -m pytest -q packages/agent-harness/tests
python packages/agent-harness/scripts/ci_check.py --offline --wheelhouse <dir-with-wheels>
```

`ci_check.py` is the same script the package's CI job runs: offline install,
package tests, cold-import fence, build wheel+sdist and run the examples
outside the source tree, resolve relative markdown links, and check the
provenance files. If it is green, the CI job has no suprises left for you.

`scripts/verify_release.py` is the release-shaped version of the same checks and
can be run directly.

## Rules this package follows

1. **A behaviour change starts with a failing test.** Add or extend a focused
   test, watch it fail for the right reason, then make the smallest change.
2. **Pure moves freeze a characterization test first.** Moving code must not
   change behaviour; pin it before and after.
3. **Contract tests over implementation tests.** The ports in `docs/contract.md`
   are the public surface; test the contract your adapter must satisfy. Several
   ports ship a *shared contract suite* that runs against both the reference
   implementation and yours (`docs/adapters.md`).
4. **Failures are explicit.** A router, provider or store that times out, is
   cancelled or returns unparseable output reports that state; it never reports
   a fabricated success and never turns `unknown` into success.
5. **No secrets, ever.** Credentials are injected by the host; tests use
   loopback fixtures. Nothing credential-shaped may reach events, the store,
   backups or a tool environment.
6. **Text encoding is declared, never inherited.** Tests spawn child processes
   through `tests/child_process.py` and name `encoding=` on file I/O; do not rely
   on the ambient locale.
7. **Line endings stay LF.** The `fixtures-for-commit` digests are blob bytes;
   see the repository's `.gitattributes`.

## Tests must stay self-contained

The package's tests may only import `yanxu_harness` and the standard library.
They must not import an application's test helpers, and the application must not
import these helpers. Helper modules under `tests/` are part of that fence.

## Reporting security issues

See `SECURITY.md`. Do not open a public issue for a vulnerability, and do not
assume a reporting address exists — none has been published yet.

## Licensing of contributions

This package is MIT. Contributions are accepted under the same license. There is
no separate contributor license agreement to sign. Third-party material that
lands in the tree must be recorded in `THIRD_PARTY_NOTICES.md` with its license
and provenance, and its license text copied into `LICENSES/`.
