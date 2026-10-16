# Eval comparison

- task verdicts: before 8 (pass rate 0.875), after 8 (pass rate 1.0)
- framework failures: before 0 infra / 0 invalid, after 0 infra / 0 invalid
- elapsed (before): n=8 min=0.01 max=0.161 mean=0.076
  - bytes: actual=0 estimated=12 unknown=0 known_subtotal=17045.0 complete=False
  - requests: actual=1 estimated=0 unknown=0 known_subtotal=1.0 complete=True
  - tokens: actual=0 estimated=0 unknown=14 known_subtotal=0.0 complete=False
- elapsed (after): n=8 min=0.012 max=0.16 mean=0.078
  - bytes: actual=0 estimated=13 unknown=0 known_subtotal=19014.0 complete=False
  - requests: actual=1 estimated=0 unknown=0 known_subtotal=1.0 complete=True
  - tokens: actual=0 estimated=0 unknown=15 known_subtotal=0.0 complete=False

| case | repeat | before | after | changed |
| --- | --- | --- | --- | --- |
| fix-calc-tests | 0 | failed | passed | yes |
| approval-write-once | 0 | passed | passed | no |
| partial-batch-evidence | 0 | passed | passed | no |
| drift-refusal | 0 | passed | passed | no |
| deadline-during-recovery | 0 | passed | passed | no |
| locate-bug | 0 | passed | passed | no |
| review-diff | 0 | passed | passed | no |
| long-context-summary-cancel | 0 | passed | passed | no |

