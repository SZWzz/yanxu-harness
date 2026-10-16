# Eval suite `harness-protocol` v1

- suite digest: `sha256:d3a7fdd93266f7e0b924124073c949c3fcef908644dbc9adaf6dddc9bf31c764`
- framework commit: `e9b394bd464cbf3840b23306610bd41a47f5c4a9`
- python: 3.12.13 on macOS-27.0.1-arm64-arm-64bit
- mode: **offline** (scripted adapter; measures the harness, not a model)

| case | repeat | outcome | verifier | elapsed | notes |
| --- | --- | --- | --- | --- | --- |
| fix-calc-tests | 0 | passed | passed | 0.02s |  |
| approval-write-once | 0 | passed | passed | 0.01s |  |
| partial-batch-evidence | 0 | passed | passed | 0.14s |  |
| drift-refusal | 0 | passed | passed | 0.11s |  |
| deadline-during-recovery | 0 | passed | passed | 0.16s |  |
| locate-bug | 0 | passed | passed | 0.01s |  |
| review-diff | 0 | passed | passed | 0.14s |  |
| long-context-summary-cancel | 0 | passed | passed | 0.06s |  |

Outcomes: cancelled=0, failed=0, infra_error=0, invalid_result=0, not_run=0, passed=8

`infra_error` and `invalid_result` are this framework's own failures and are **not** counted as the task failing.
