# Eval comparison

- task verdicts: before 8 (pass rate 0.875), after 8 (pass rate 1.0)
- framework failures: before 0 infra / 0 invalid, after 0 infra / 0 invalid
- elapsed (before, paired samples only): n=8 min=0.011 max=0.161 mean=0.077
  - bytes: actual=0 estimated=12 unknown=0 known_subtotal=17045.0 complete=False
  - requests: actual=1 estimated=0 unknown=0 known_subtotal=1.0 complete=True
  - tokens: actual=0 estimated=0 unknown=14 known_subtotal=0.0 complete=False
- elapsed (after, paired samples only): n=8 min=0.012 max=0.164 mean=0.081
  - bytes: actual=0 estimated=13 unknown=0 known_subtotal=19014.0 complete=False
  - requests: actual=1 estimated=0 unknown=0 known_subtotal=1.0 complete=True
  - tokens: actual=0 estimated=0 unknown=15 known_subtotal=0.0 complete=False
- raw observations (before): n=8 min=0.011 max=0.161 mean=0.077
- raw observations (after): n=8 min=0.012 max=0.164 mean=0.081
- samples (before): listed=8 valid=8 paired=8 duplicates=0 not_run=0 infra_or_invalid=0
- samples (after): listed=8 valid=8 paired=8 duplicates=0 not_run=0 infra_or_invalid=0

| case | samples before | samples after | not run (b/a) | infra or invalid (b/a) |
| --- | --- | --- | --- | --- |
| approval-write-once | 1 | 1 | 0/0 | 0/0 |
| deadline-during-recovery | 1 | 1 | 0/0 | 0/0 |
| drift-refusal | 1 | 1 | 0/0 | 0/0 |
| fix-calc-tests | 1 | 1 | 0/0 | 0/0 |
| locate-bug | 1 | 1 | 0/0 | 0/0 |
| long-context-summary-cancel | 1 | 1 | 0/0 | 0/0 |
| partial-batch-evidence | 1 | 1 | 0/0 | 0/0 |
| review-diff | 1 | 1 | 0/0 | 0/0 |

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

