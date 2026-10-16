# Eval suite `harness-protocol` v1

- suite digest: `sha256:e00b4edeca481353f3a35d43b9219a0fc1b53f470dee912930aab42d3b8a8806`
- framework commit: `cce8aa542fe28e61610f2a7b50efbd326c9f764f` (dirty)
- python: 3.12.13 on macOS-27.0.1-arm64-arm-64bit
- mode: **offline** (scripted adapter; measures the harness, not a model)

| case | repeat | outcome | verifier | elapsed | notes |
| --- | --- | --- | --- | --- | --- |
| fix-calc-tests | 0 | passed | passed | 0.04s |  |
| approval-write-once | 0 | passed | passed | 0.04s |  |
| partial-batch-evidence | 0 | passed | passed | 0.14s |  |
| drift-refusal | 0 | passed | passed | 0.13s |  |
| deadline-during-recovery | 0 | passed | passed | 0.19s |  |

Outcomes: cancelled=0, failed=0, infra_error=0, invalid_result=0, not_run=0, passed=5

`infra_error` and `invalid_result` are this framework's own failures and are **not** counted as the task failing.

