# Diagnostic samples

`partial-recovery-report.json` / `.md`: a **real** partial-batch recovery, read by the
read-only inspector. The run is the one the recovery tests use: a child process dies with
call A settled and committed, call B's intent open, call C never dispatched; a second
process resumes, classifies every call against the operation ledger, and stops with a
reconciliation question for B.

It is generated (not hand-written) with the repository's own fixtures:

```python
paths = partial-batch-then-die (tests/fault_windows.py)      # the real crash
harness.resume("r1", {}, run_context("a2"))                  # the real recovery → suspended
RunInspector(persistence=SqlitePersistence(runs, read_only=True),
             ledger=SqliteExecutionBackend(operations, read_only=True))
    .inspect_run(RunIdentity(namespace="project-a", session_id="s1", run_id="r1", attempt_id="a3"))
# then to_json() / to_markdown()
```

What the sample is here to show:

* **cross-attempt attribution** — `a1` ran the calls, `a2` recovered, `a3` read the report;
  the reader's own attempt is never merged with the work;
* **where a receipt lives** — A is `receipt: … 'succeeded'`, B is `receipt: … 'running'`
  (an open intent the ledger cannot decide), and the report does not copy the ledger;
* **why the run is waiting** — `reconciliation` on `c-notes.b`, from the run's own record;
* **what is unknown** — no usage was reported (`unknown` with the reason, not `0`), and the
  tool calls with no observation are reported as *missing* spans rather than as fast ones;
* **what cannot be proven** — the report lists the in-process counters it cannot see;
* **the boundary it was read at** — `boundary` names the read start, the scanned high water, the
  cursor a continuation would use, the run revision, and `exhausted: true`, which says the scan
  reached the end of the store rather than only the end of what it was allowed to carry. The
  reader that produced this sample followed its own cursor to the end and found nothing more.

The JSON is the fact format; the Markdown is a view of the same report, not a second pass
over the stores.

## Task diagnostics sample

`task-diagnostics.json` / `.md`: one persistent task's control facts, read by the read-only
`diagnostics/tasks.py#TaskInspector`. It is generated (not hand-written) with
`scripts/regenerate_task_diagnostics_sample.py`, which seeds a real SQLite control store and then
inspects it through a **read-only** connection.

What the sample is here to show (plan §11.1):

* **the task/run/attempt link** — the task record is separate from its run, and the current attempt
  is named;
* **input application** — each input's kind, sequence, state and the run revision that applied it;
  the initial input is applied by the run's first transaction;
* **the scheduling claim** — holder, attempt, fence and liveness, read without renewing it;
* **child relations** — the relation, its authorization digest, the child's committed status and
  outcome, and the verifier's acceptance;
* **budget reservations** — unit, amount, state and settled amount, with the dispatch identity
  digested rather than published;
* **notification provenance** — the control outbox entries, whether each was delivered, and the
  payload digest; a pending notice is visible as pending;
* **the public whitelist** — no raw task text, no child result content and no absolute workspace
  path: they are replaced by a digest and a path tail.
