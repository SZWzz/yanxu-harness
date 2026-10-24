# Repository task knowledge

## Project boundaries

- This package provides reusable harness mechanisms. Keep product policy, UI, web/IDE flows, shell interpreters, production sandboxes, and worktree lifecycle code in the embedding host.
- Preserve the single execution path through the existing Core `Runner`, `DefaultHarness`, and `ToolGateway`. Do not add a second model/tool loop or a second side-effect ledger.
- Task/Run execution facts remain owned by the existing Task control store and Harness persistence. New orchestration views may reference those facts but must not fabricate or overwrite them.
- A plan approval never grants tool permission. Existing Gateway policy and operation recovery remain authoritative; `unknown` must not become success.

## Compatibility and implementation

- Support Python 3.10+. The base install stays standard-library-only; optional integrations belong in explicit extras and must import lazily.
- Preserve existing public APIs and old non-orchestrated Task/Run behavior. Put optional capabilities behind explicit ports and refuse unsupported backends clearly.
- Persisted formats need independent schema versions and safe refusal for unknown versions. Read-only paths must not create stores or migrate schemas.
- Follow `CONTRIBUTING.md`: behavior changes start with a focused failing test; contract tests should cover each reference backend; report the commands and outcomes actually observed.
- Update the public contract, API snapshots, documentation index, and verification mapping when public behavior changes.
- Keep user changes intact. Do not rewrite historical release records or publish packages unless the user explicitly asks.
