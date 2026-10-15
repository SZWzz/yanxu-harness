# Supported versions and dependencies

> Status: **local candidate, not published.** Nothing here is a release
> guarantee yet; it records what has actually been built and run.

## Python

Evidence recorded on 2026-10-15 for the 0.1.0a1 tree in this repository. "Built
and run" means `python -m pytest -q tests` in a dedicated virtual environment plus
the four examples; the release numbers come from `scripts/verify_release.py`
(wheel and sdist, each installed into a venv outside the source tree).

| Python | Status |
| --- | --- |
| 3.10.x | declared floor; **not executed on the machine that produced this table** (no 3.10 interpreter is installed there). The floor is only checked statically — see below |
| 3.11.15 | built and run: package suite **562 passed / 1 skipped**, all four examples OK. Release verification was not re-run on 3.11 in this round |
| 3.12.13 | built and run: package suite **562 passed / 1 skipped**, all four examples OK. Release verification: wheel and sdist each built, installed into a throwaway venv outside the source tree and run from there — **538 passed / 21 skipped** in both cases (the extra skips are the checkout-only gates: packaging metadata and build scripts are not part of an installed artifact) |
| 3.13+ | not declared, not tested |
| < 3.10 | unsupported; `requires-python = ">=3.10"` |

The floor is machine-checked, not asserted in prose: `tests/test_python_floor.py`
compiles every source file with `ast.parse(feature_version=(3, 10))`, scans for
3.11+/3.12+ only APIs, and cross-checks the extras' environment markers. Static
agreement is not the same claim as a run on 3.10, and this table does not pretend
otherwise.

## Platforms

| Platform | Status |
| --- | --- |
| macOS (arm64) | the only platform these results were produced on |
| Linux / Windows | present in the CI matrix, but the first CI run never started (GitHub billing/limit), so they have **never actually run** — treat as unverified |

## Runtime dependencies

The base install has **no third-party runtime dependency**: the model loop,
storage ports, SQLite reference store, skills loader, decision ports and
provider protocol use the standard library only.

| Extra | Pulls in | Needed for | Excluded from the base? |
| --- | --- | --- | --- |
| *(none)* | — | the whole base package; a host that configures no optional adapter works | base |
| `pydantic` | `pydantic>=2` | tool argument models (`args_model`) | yes — an adapter that never validates arguments does not need it |
| `yaml` | `PyYAML` | `SKILL.md` front matter parsing | yes — an in-memory skill provider needs no parser |
| `dev` | pytest, build, … | running the tests and building the distributions | yes |

A missing optional extra can only break the adapter that asked for it; importing
`yanxu_harness` itself must not pull in any of them (`tests/test_import_fence.py`).

## What "verified" means here

* `tests/` in this package import nothing from another repository, and a host that
  embeds the harness never imports this package's helpers.
* Release verification installs the built wheel/sdist into a throwaway virtual
  environment **outside the source tree** with `--no-index`, then asserts the
  imported file is not the one in the checkout.
* The numbers above are produced by `scripts/verify_release.py`,
  `scripts/ci_check.py` and `python -m pytest -q tests`.
