# Eval suite `harness-protocol` v1

- suite digest: `sha256:36c1e1d612223e16683268f3babd1eff475a4de5257127df875b134b6510a45c`
- framework commit: `90cc7cb6b22548863bff6dfe6df5e62bf52cdc39` (dirty)
- python: 3.12.13 on macOS-27.0.1-arm64-arm-64bit
- mode: **offline** (scripted adapter; measures the harness, not a model)

| case | repeat | outcome | verifier | elapsed | notes |
| --- | --- | --- | --- | --- | --- |
| fix-calc-tests | 0 | passed | passed | 0.03s |  |
| approval-write-once | 0 | passed | passed | 0.03s |  |
| partial-batch-evidence | 0 | passed | passed | 0.15s |  |
| drift-refusal | 0 | passed | passed | 0.13s |  |
| deadline-during-recovery | 0 | passed | passed | 0.19s |  |

Outcomes: cancelled=0, failed=0, infra_error=0, invalid_result=0, not_run=0, passed=5

`infra_error` and `invalid_result` are this framework's own failures and are **not** counted as the task failing.

