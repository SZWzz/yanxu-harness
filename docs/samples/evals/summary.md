# Eval suite `harness-protocol` v1

- suite digest: `sha256:3aeb35bf45bfd11c027d86862cbfd8659b24472271a9d89f6c8bcee18fbb81d1`
- framework commit: `ae3bcf34ac8d9d325cfb2215fe52cdfbac7b4911` (dirty)
- python: 3.12.13 on macOS-27.0.1-arm64-arm-64bit
- mode: **offline** (scripted adapter; measures the harness, not a model)

| case | repeat | outcome | verifier | elapsed | notes |
| --- | --- | --- | --- | --- | --- |
| fix-calc-tests | 0 | passed | passed | 0.03s |  |
| approval-write-once | 0 | passed | passed | 0.03s |  |
| partial-batch-evidence | 0 | passed | passed | 0.14s |  |
| drift-refusal | 0 | passed | passed | 0.13s |  |
| deadline-during-recovery | 0 | passed | passed | 0.22s |  |
| locate-bug | 0 | passed | passed | 0.06s |  |
| review-diff | 0 | passed | passed | 0.17s |  |
| long-context-summary-cancel | 0 | passed | passed | 0.07s |  |

Outcomes: cancelled=0, failed=0, infra_error=0, invalid_result=0, not_run=0, passed=8

`infra_error` and `invalid_result` are this framework's own failures and are **not** counted as the task failing.

