# Eval suite `harness-protocol` v1

- suite digest: `sha256:465854f5ba60c2132790ee2248547ba77c8f4e7cc0729a75c79ceac98552954f`
- framework commit: `4b832c2245c492394c4ac43f54d5954e956951e1` (dirty)
- python: 3.12.13 on macOS-27.0.1-arm64-arm-64bit
- mode: **offline** (scripted adapter; measures the harness, not a model)

| case | repeat | outcome | verifier | elapsed | notes |
| --- | --- | --- | --- | --- | --- |
| fix-calc-tests | 0 | passed | passed | 0.03s |  |
| approval-write-once | 0 | passed | passed | 0.05s |  |
| partial-batch-evidence | 0 | passed | passed | 0.14s |  |
| drift-refusal | 0 | passed | passed | 0.15s |  |
| deadline-during-recovery | 0 | passed | passed | 0.29s |  |
| locate-bug | 0 | passed | passed | 0.03s |  |
| review-diff | 0 | passed | passed | 0.16s |  |
| long-context-summary-cancel | 0 | passed | passed | 0.07s |  |

Outcomes: cancelled=0, failed=0, infra_error=0, invalid_result=0, not_run=0, passed=8

`infra_error` and `invalid_result` are this framework's own failures and are **not** counted as the task failing.

