# Changelog

All notable changes to this package are recorded here. This project is an alpha:
breaking changes are allowed between versions, and **persistent formats still
require a migration or an explicit refusal** — an alpha is not a licence to drop
a user's existing session (see `docs/contract.md` and `docs/recovery.md`).

## 0.1.0a1 — local candidate, not published

First working version. There is no release date for a published artifact because
nothing has been published: the source repository exists, but there is no package
index, no release account behind an upload and no tag. The distribution name
`yanxu-harness`, the import name `yanxu_harness` and the version string are this
package's own choices, not verified names on an index.

What is in it:

* **One model loop** (`yanxu_harness.core`): neutral messages, streaming events,
  tool batches, turn budgets, steering/follow-up queues, cancellation. No second
  implementation hides behind a shim — compatibility paths re-export the same
  objects.
* **Run lifecycle** (`yanxu_harness.runtime`): run identity, principal,
  cancellation tokens and a suspend/resume contract with versioned state.
* **Tools in four layers** (`yanxu_harness.tools`): definition+registry,
  deployment visibility, per-turn declaration/discovery, and
  authorization+execution through a single gateway. Discovery takes effect on the
  *next* batch and grants nothing; rejected calls leave no intent behind.
* **Skills in three layers** (`yanxu_harness.skills`): metadata catalog,
  on-demand body/resources, session-bound version fingerprints. Skill content is
  untrusted guidance and cannot authorize or execute anything.
* **Optional decision routing** (`yanxu_harness.decisions`): no-op, rule and
  model-backed routers behind one port, with bounded payloads, a call budget, a
  cache and per-kind validation. A host that configures no decision model still
  runs every task.
* **Context management** (`yanxu_harness.context`): the raw transcript archive
  and the model projection are separate; summarization is a budgeted view and
  never rewrites the archived history; a final payload gate measures the exact
  serialized bytes before dispatch.
* **Persistence and recovery** (`yanxu_harness.storage`): store and
  unit-of-work ports with in-memory and SQLite reference implementations,
  operation intent/receipts, approval binding, lease/CAS fencing and
  cross-process fault windows. External side effects are reported as `unknown`
  when they cannot be proven, and are never retried blindly.
* **Providers** (`yanxu_harness.providers`): a provider protocol plus explicitly
  configured transports. Credentials come from the host; the package ships no
  network client of its own.
* **Examples**: basic dialogue, an approved data edit, cross-process recovery,
  and a non-fiction pipeline combining custom tools, a skill and an optional
  decision router.

Not in this version (deliberate): automatic skill marketplaces, remote plugin
installation, distributed scheduling, multi-agent swarms, automatic provider
switching and production sandboxing. Extension points are kept, but no generic
platform is built ahead of need.

## Format changes

| Version | Change |
| --- | --- |
| 0.1.0a1 | initial event/checkpoint/schema versions; unknown or future versions are refused rather than guessed |
