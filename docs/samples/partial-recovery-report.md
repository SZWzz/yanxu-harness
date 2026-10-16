# Run `r1`

- identity: `project-a` / `s1`
- status: **suspended**
- read by attempt: `a3`
- attempts that did the work: `a2`, `a1`
- waiting for: `reconciliation` on `c-notes.b`
- read boundary: cursor `-1` → `5` (high water `5`), run revision `6`, 1 page(s)

## Tool calls

| call | tool | outcome | progress | operation | receipt |
| --- | --- | --- | --- | --- | --- |
| c-notes.a | notes.a | settled | observed | project-a:s1:r1:request:1:c-notes.a | receipt: the ledger records this operation as 'succeeded' |
| c-notes.b | notes.b | uncertain | observed | project-a:s1:r1:request:1:c-notes.b | receipt: the ledger records this operation as 'running' |
| c-notes.c | notes.c | unstarted | declared |  | none |

## Stages

| stage | attempt | started | duration | note |
| --- | --- | --- | --- | --- |
| model:request | a1 | yes | 0.1 ms |  |
| tool:notes.a | a1 | yes | 0.3 ms |  |
| tool:batch | a1 | yes | not measured | a wall-clock span across records, not a measured duration; the calls inside it have their own spans and are never summed into this one |
| tool:notes.c | unknown | missing | missing | no observation was recorded for this call, so it has no span |

## Measurement

| unit | kind | amount | source | reference | note |
| --- | --- | --- | --- | --- | --- |
| bytes | estimated | 510 (estimated) | committed | events:2 | the size of the request body this build handed the provider |
| tokens | unknown | unknown | missing | events:2 | the provider did not report usage for this request |

- `bytes`: known subtotal 510, 0 unknown, 1 estimated — **not a complete total**
- `tokens`: known subtotal 0, 1 unknown, 0 estimated — **not a complete total**

## Timeline

| reference | kind | attempt | durable | at |
| --- | --- | --- | --- | --- |
| events:0 | run\_started | a1 | yes | 1792131661.802 |
| events:1 | model\_request\_dispatched | a1 | yes | 1792131661.803 |
| events:2 | model\_request\_completed | a1 | yes | 1792131661.803 |
| events:3 | tool\_call\_observed | a1 | yes | 1792131661.804 |
| events:4 | approval\_requested | unknown | yes | 1792131661.809 |
| events:5 | run\_suspended | unknown | yes | 1792131661.809 |
| run:r1 | explanation:outcome | derived | no |  |
| project-a:s1:r1:request:1:c-notes.b:reconciliation | explanation:waiting | derived | no |  |

## Unsupported here

- in-process facts (lease/CAS refusals and event-fanout failures) are not durable; a store-only report cannot show them

## Sources

committed=15, conflict=0, derived=2, missing=3, observed=0, receipt=2, unsupported=0

