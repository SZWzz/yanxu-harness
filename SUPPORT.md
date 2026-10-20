# Supported versions and dependencies

> Status: **public alpha, `0.3.0a1`** (2026-10-20). No package-index release has
> been published. This file distinguishes declared compatibility from actual
> execution evidence; alpha APIs and persistence formats may change.

## Getting help

Report bugs in [Issues](https://github.com/SZWzz/yanxu-harness/issues), ask
integration questions in [Discussions](https://github.com/SZWzz/yanxu-harness/discussions),
and report vulnerabilities privately through [SECURITY.md](SECURITY.md).
Include the package version, Python/OS, enabled extras and a minimal reproduction
with credentials and private data removed. Maintenance focuses on the current
alpha; there is no response-time guarantee or separate older-alpha support branch.

## Python

The declared floor remains Python 3.10. The current `0.3.0a1` release evidence
was produced on Python 3.12 / macOS arm64; see the [release record](docs/release-0.3.0a1.zh-CN.md)
and [verification history](docs/verification.md). Older interpreter runs belong
to the version actually tested and do not certify the current release.

| Python | Status |
| --- | --- |
| 3.10 | declared floor; `0.3.0a1` has static floor/import checks and a resolved MCP lock, but no current-version interpreter run. Historical `0.1.0a2` evidence: Python 3.10.14, 657 passed / 1 skipped and offline gate 6/6 |
| 3.11 | declared; no current-version interpreter run. Historical `0.1.0a2` evidence: Python 3.11.15, 657 passed / 1 skipped and installed-artifact examples |
| 3.12 | `0.3.0a1` wheel and sdist each installed outside the source tree: 1362 passed / 36 skipped / 0 failed; MCP-specific suites each 92 passed / 0 skipped. Base cold-import and example gates passed |
| 3.13+ | not declared, not tested |
| < 3.10 | unsupported; `requires-python = ">=3.10"` |

The historical 3.10 execution and the current static checks are separate evidence.
`tests/test_python_floor.py` compiles every source file with
`ast.parse(feature_version=(3, 10))`, scans for 3.11+/3.12+ only APIs, and
cross-checks the extras' environment markers. Static parsing and dependency
resolution do not replace execution on the declared interpreter.

## Platforms

| Platform | Status |
| --- | --- |
| macOS (arm64) | current local release verification environment |
| Linux / Windows | configured in the CI matrix; no successful current-release execution evidence at the public-source audit baseline. Treat as unverified until an actual run passes |

The [Actions page](https://github.com/SZWzz/yanxu-harness/actions) contains live
workflow results. A configured matrix or a job that failed before executing steps
does not establish platform support. The [public-source audit](docs/public-source-audit.zh-CN.md)
records the exact historical runs inspected on 2026-10-20.

## Runtime dependencies

The base install has **no third-party runtime dependency**: the model loop,
storage ports, SQLite reference store, skills loader, decision ports and
provider protocol use the standard library only.

| Extra | Pulls in | Needed for | Excluded from the base? |
| --- | --- | --- | --- |
| *(none)* | — | the whole base package; a host that configures no optional adapter works | base |
| `pydantic` | `pydantic>=2` | tool argument models (`args_model`) | yes — an adapter that never validates arguments does not need it |
| `yaml` | `PyYAML` | `SKILL.md` front matter parsing | yes — an in-memory skill provider needs no parser |
| `mcp` | `mcp==2.3.0`, `jsonschema>=4.20.0` | MCP Tools over stdio and Streamable HTTP; see the [adapter guide](docs/mcp-client.zh-CN.md) and [SDK baseline](docs/mcp-client-sdk-baseline.zh-CN.md) | yes — imported only when the adapter is explicitly used |
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
