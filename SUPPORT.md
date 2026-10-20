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

The declared floor remains Python 3.10. The original local `0.3.0a1` release
artifacts were verified on Python 3.12 / macOS arm64; see the [release record](docs/release-0.3.0a1.zh-CN.md).
Subsequent public-source CI built and tested revision `15c4a5b` on the environments
below. These later builds do not replace or change the hashes of the original
release artifacts. The [verification history](docs/verification.md) keeps older
interpreter runs attached to the version actually tested.

| Python | Status |
| --- | --- |
| 3.10 | declared floor; public-source revision `15c4a5b` passed the complete Linux gate, including new wheel/sdist builds and installed-artifact tests. The original pinned release artifacts were not reverified on 3.10 |
| 3.11 | public-source revision `15c4a5b` passed the complete Linux gate, including new wheel/sdist builds and installed-artifact tests. The original pinned release artifacts were not reverified on 3.11 |
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
| macOS (arm64) | original local release verification environment; public-source CI on `macos-latest` also passed the complete gate |
| Linux | public-source CI passed the complete gate on Python 3.10, 3.11 and 3.12; the 50ms evaluation instability below remains recorded |
| Windows | PR #2 exercises the real `windows-latest` release gate and fixes the reported portability failures, but recent runs still fail in varying frozen 50ms deadline evaluations. No Windows release-gate attempt has completed cleanly yet; do not treat this platform as verified |

## Public-source CI (2026-10-20)

Source revision: `15c4a5bfde212ecfe6454b893730e075b66ebcdf` (`0.3.0a1`).
The [push workflow](https://github.com/SZWzz/yanxu-harness/actions/runs/39358744179)
passed all six gate steps in each of these jobs:

| Environment | Evidence |
| --- | --- |
| Linux / Python 3.10 | [successful job](https://github.com/SZWzz/yanxu-harness/actions/runs/39358744179/job/118419957205) |
| Linux / Python 3.11 | [successful job](https://github.com/SZWzz/yanxu-harness/actions/runs/39358744179/job/118419957225) |
| Linux / Python 3.12 | [successful job](https://github.com/SZWzz/yanxu-harness/actions/runs/39358744179/job/118419957017) |
| macOS / Python 3.12 | [successful job](https://github.com/SZWzz/yanxu-harness/actions/runs/39358744179/job/118419957219) |

Each base suite reported **1308 passed / 13 skipped**; release verification then
installed the optional extras and ran the copied suite, all twelve offline examples,
evaluation and diagnostics against new wheel/sdist installations.

**Known instability:** the same revision's [parallel PR Linux/Python 3.12 job](https://github.com/SZWzz/yanxu-harness/actions/runs/39358747581/job/118420010182)
passed its base suite but failed one installed-artifact test,
`test_the_long_context_case_compacts_and_each_repeat_gets_its_own_allowance`.
It uses a 50ms run deadline; the first repeat failed while the second passed.
The same-revision push job and local focused reproduction passed. This is recorded
as an intermittent evaluation failure, not a fixed defect or an entirely green
workflow. Windows failures are tracked separately and have not been waived.

**Windows compatibility follow-up:** PR #2 ([head `af9de51`](https://github.com/SZWzz/yanxu-harness/pull/2)) now runs the full job on `windows-latest`; the original path, newline, socket, process cleanup, and resource-lifetime failures no longer appear in the latest jobs. The remaining failures vary across the fixed 50ms cases: [`test_the_long_context_case_compacts_and_each_repeat_gets_its_own_allowance`](https://github.com/SZWzz/yanxu-harness/actions/runs/39370845757/job/118458136127), [`long-context-summary-cancel`](https://github.com/SZWzz/yanxu-harness/actions/runs/39370845757/job/118461264746), and [`deadline-during-recovery`](https://github.com/SZWzz/yanxu-harness/actions/runs/39373660633/job/118466861717) / [latest attempt](https://github.com/SZWzz/yanxu-harness/actions/runs/39374962486/job/118470939684). The recovery case's latest detailed record has `max_runtime_exceeded` with no `probe.c` admission before the 50ms deadline; the subsequent Windows-only retry instead failed in `long-context-summary-cancel` ([job](https://github.com/SZWzz/yanxu-harness/actions/runs/39374962486/job/118473878804)). These attempts show timing-sensitive evaluation failures, not a stable Windows pass. The cases, budgets and verifiers remain unchanged.

The [Actions page](https://github.com/SZWzz/yanxu-harness/actions) contains live
workflow results. A configured matrix or a job that failed before executing steps
does not establish platform support. The [public-source audit](docs/public-source-audit.zh-CN.md)
records the historical runs, first failures, fixes and later job-level evidence.

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
