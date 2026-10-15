# Supported versions and dependencies

> Status: **local candidate, not published.** Nothing here is a release
> guarantee yet; it records what has actually been built and run.

## Python

| Python | Status |
| --- | --- |
| 3.10.14 | built and run: package suite **505 passed / 1 skipped**, release verification (wheel + sdist, installed outside the source tree) **497 passed / 9 skipped**, all four examples OK |
| 3.11.14 | same as 3.10.14 |
| 3.12.13 | same; this is the development interpreter |
| 3.13+ | not declared, not tested |
| < 3.10 | unsupported; `requires-python = ">=3.10"` |

The floor is machine-checked, not asserted in prose: `tests/test_python_floor.py`
compiles every source file with `ast.parse(feature_version=(3, 10))`, scans for
3.11+/3.12+ only APIs, and cross-checks the extras' environment markers.

## Platforms

| Platform | Status |
| --- | --- |
| macOS (arm64) | the only platform these results were produced on |
| Linux / Windows | present in the CI matrix, **never actually run** here — treat as unverified |

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

* `tests/` in this package never import the host application, and the host's
  tests never import this package's helpers.
* Release verification installs the built wheel/sdist into a throwaway virtual
  environment **outside the source tree** with `--no-index`, then asserts the
  imported file is not the one in the checkout.
* The numbers above are produced by `scripts/verify_release.py`,
  `scripts/ci_check.py` and `python -m pytest -q packages/agent-harness/tests`.
