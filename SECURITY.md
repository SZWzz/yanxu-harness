# Security policy

> Status: **local candidate, not published.** There is no public repository, no
> release account and **no security reporting channel** yet. This file records
> the security model and states plainly what does not exist; it does not invent
> an address or a service.

## Reporting a vulnerability

Do **not** open a public issue for a suspected vulnerability, and do not assume
an inbox exists: none has been published.

When the repository and its maintainer channel are created, this file will name
the actual channel. Until then, report privately to whoever handed you this
checkout. If you cannot reach anyone privately, keep the report local rather
than publishing exploit details — this is an alpha with no deployed instances.

Please include: the version string, a minimal reproduction, and whether the
issue can be triggered without a hostile provider or a modified project
database.

## Supported versions

`0.1.0a1` only. It is an alpha working version, not a release. No older version
is maintained because none has been published.

## What counts as a security issue here

This package makes a small number of strong promises. Breaking any of them is a
security issue:

1. **Credentials and private provider frames must not reach public state.**
   Events, the SQLite store, backups and tool environments must never contain
   provider secrets, raw private frames or resume signatures. Public payload
   guards reject them at the boundary.
2. **A write must not happen without its approval.** Approvals are bound to the
   namespace, call id, complete argument hash, target version, tool contract
   version, policy version and expiry; any drift must invalidate the approval
   rather than execute it.
3. **`unknown` must never be reported as success or retried automatically.**
   An external effect that cannot be proven is `unknown`; the harness does not
   fabricate a receipt for it.
4. **Skills must never grant authority or execute code.** Skill text and bundled
   resources are untrusted guidance: reading a skill must not produce a
   permission, an approval, or a script execution.
5. **Project isolation.** A `project_id` boundary must not leak records, files,
   approvals or operations into another project, including through backups.
6. **One loop.** Compatibility shims must re-export the same functions, types and
   exceptions, so an `except` clause or an `isinstance` check cannot split
   between two implementations.

## What this package does not promise

* It does not sandbox a hostile host: a host can hand the harness a provider
  call, a tool handler or a storage implementation that does the wrong thing.
* It does not provide exactly-once execution for external side effects. It
  provides intent, receipts, fencing and reconciliation; a caller that needs
  exactly-once must supply an idempotent backend and reconcile.
* It does not treat model output as trustworthy: model-provided text is bounded
  and validated, and can never change tool authority.
