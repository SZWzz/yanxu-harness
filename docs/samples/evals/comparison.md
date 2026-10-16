# Eval comparison

- task verdicts: before 1 (pass rate 0.0), after 1 (pass rate 1.0)
- framework failures: before 0 infra / 0 invalid, after 0 infra / 0 invalid
- elapsed (before): n=1 min=0.025 max=0.025 mean=0.025
  - bytes: actual=0 estimated=2 unknown=0 known_subtotal=2323.0 complete=False
  - tokens: actual=0 estimated=0 unknown=2 known_subtotal=0.0 complete=False
- elapsed (after): n=1 min=0.03 max=0.03 mean=0.03
  - bytes: actual=0 estimated=2 unknown=0 known_subtotal=2526.0 complete=False
  - tokens: actual=0 estimated=0 unknown=2 known_subtotal=0.0 complete=False

| case | repeat | before | after | changed |
| --- | --- | --- | --- | --- |
| fix-calc-tests | 0 | failed | passed | yes |

