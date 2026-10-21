# Contributing

> Status: **public alpha release, `0.4.0a1`.** Source and contributions are hosted at
> <https://github.com/SZWzz/yanxu-harness>.
> APIs and persistence formats may change between alphas, with migration or
> explicit refusal where required by the public contract.

## Feedback and pull requests

Use [Issues](https://github.com/SZWzz/yanxu-harness/issues) for reproducible bugs
and feature requests, and [Discussions](https://github.com/SZWzz/yanxu-harness/discussions)
for integration questions. Include the package version, Python/OS, expected and
actual behavior, and a minimal reproduction without credentials or private data.
For a substantial API change, describe the proposed contract in an issue first.

Keep pull requests focused, explain the resulting behavior, and list the checks
actually run, including failures or unverified environments. Maintainers review
contributions as time permits; this alpha has no guaranteed triage or response time.
Start with the [documentation index](docs/README.md); implementation plans and old
review findings are historical evidence, not promises that every proposed feature ships.

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

For MCP adapter changes, install `'.[dev,yaml,pydantic,mcp]'` and also run
`python -m pytest -q tests/mcp`. The base environment intentionally skips MCP
tests when the optional SDK is absent; that skip does not verify the adapter.

## The gate you must run

For the PyPI account setup and release workflow, see the
[publishing guide](docs/pypi-publishing.zh-CN.md) (Chinese).

```bash
python -m pytest -q tests
python scripts/ci_check.py --prepare-wheelhouse build-wheelhouse   # the only network step
python scripts/ci_check.py --offline --wheelhouse build-wheelhouse
```

`ci_check.py` is the same script the package's CI job runs: offline install,
package tests, cold-import fence, build wheel+sdist and run the examples
outside the source tree, resolve relative markdown links, and check the
provenance files. If it is green, the CI job has no surprises left for you.
`--prepare-wheelhouse` reads the requirement list from `pyproject.toml` itself, so
a raised floor cannot leave a stale copy behind.

`scripts/verify_release.py` is the release-shaped version of the same checks and
can be run directly:

```bash
python scripts/verify_release.py --offline --wheelhouse <absolute-or-relative-dir> --out build/release
```

`--out` copies the wheel, the sdist, a `SHA256SUMS` list and a
`verification-report.md` into that directory. The report records the environment,
the digests and what was checked — including that the wheel carries no
`tests/`, `examples/`, `scripts/` or `docs/` entries and that the sdist carries
all of them, which is the convention `MANIFEST.in` and the snapshot whitelist
share.

## Rules this package follows

1. **A behaviour change starts with a failing test.** Add or extend a focused
   test, watch it fail for the right reason, then make the smallest change.
   `docs/verification.md` records which test covers which failure window; a change
   that moves a window should move its row too (a test asserts the document only
   names tests that exist).
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
7. **Line endings stay LF.** Snapshot digests cover file bytes; checkout line-ending
   conversion changes those bytes and must not be mistaken for the recorded source.

## Tests must stay self-contained

The package's tests may only import `yanxu_harness` and the standard library.
They must not import an application's test helpers, and the application must not
import these helpers. Helper modules under `tests/` are part of that fence.

## Reporting security issues

Use the [private vulnerability report form](https://github.com/SZWzz/yanxu-harness/security/advisories/new),
as described in [SECURITY.md](SECURITY.md). Do not open a public issue for a vulnerability.

## Licensing of contributions

This package is MIT. Contributions are accepted under the same license. There is
no separate contributor license agreement to sign. Third-party material that
lands in the tree must be recorded in `THIRD_PARTY_NOTICES.md` with its license
and provenance, and its license text copied into `LICENSES/`.
