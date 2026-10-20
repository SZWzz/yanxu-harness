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
| Linux | public-source CI passed the complete gate on Python 3.10, 3.11 and 3.12; the 50 ms evaluation instability recorded below predates the version-2 deadline cases and is fixed there |
| Windows | public-source CI passed the complete gate on `windows-latest` / Python 3.12 at `b06c6b2`, twice (push and PR): the source suite, then wheel/sdist installed outside the tree with the offline examples. Earlier Windows runs failed on portability defects and on a frozen 50 ms deadline that assumed a machine speed; both are fixed and documented below. The Windows job logs themselves are not publicly downloadable |

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

**Known instability (history):** the same revision's [parallel PR Linux/Python 3.12 job](https://github.com/SZWzz/yanxu-harness/actions/runs/39358747581/job/118420010182)
passed its base suite but failed one installed-artifact test,
`test_the_long_context_case_compacts_and_each_repeat_gets_its_own_allowance`.
It used a 50 ms run deadline; the first repeat failed while the second passed.
The cause was the deadline case, not the machine: version 1 gave the entire context projection
and recovery 50 ms (see the Windows follow-up below). The case is now version 2 with a measured
budget, and the recorded failure stays as history rather than being retried away.

**Windows compatibility follow-up:** PR #2 now passes the full job on `windows-latest`. At head
[`b06c6b2`](https://github.com/SZWzz/yanxu-harness/pull/2) the push run
[39384654039](https://github.com/SZWzz/yanxu-harness/actions/runs/39384654039) and the PR run
[39384659647](https://github.com/SZWzz/yanxu-harness/actions/runs/39384659647) both finished
**7/7 jobs successful**, including the Windows Python 3.12 job
([push](https://github.com/SZWzz/yanxu-harness/actions/runs/39384654039/job/118501497695),
[PR](https://github.com/SZWzz/yanxu-harness/actions/runs/39384659647/job/118501515924)) running the
source suite, the wheel/sdist installation outside the tree and the offline examples. The earlier
path, newline, socket, process-cleanup and resource-lifetime failures no longer appear.

Two problems behind the remaining Windows failures were fixed rather than retried. The
fault-injection child exited `9` on *every* path, so a child that raised before its probe (or
finished normally) was recorded as an injected crash; only the probe handler uses that code now,
and the parent verifies the declared window against the real ledger and trace before recovering.
And both deadline cases gave the whole recovery 50 ms, which assumed the *preparation* finished
in 50 ms on every host; they are now version-2 cases with a fixed, measured 2 s budget whose target
operation blocks longer than the whole budget, so the deadline — not machine speed — is what ends
the run. Version 1 is kept as history and its results are not comparable with version 2. See the
[public-source audit](docs/public-source-audit.zh-CN.md#7-windows-ci-修复收尾根因证据与回归pr-2b06c6b2)
for the root-cause table, the measured stage timings, the bounded repeat sample and the remaining
risks.

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
