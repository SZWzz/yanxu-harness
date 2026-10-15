# Core tests (copied from the host suite)

These are the host's `tests/agent_core/` characterization tests with the imports
rewritten from `backend.agent_core` to `yanxu_harness.core`. They exist so the
package proves its own loop/queue/cancellation behaviour without the host
repository, and so a shim that silently changed behaviour would fail in both
suites rather than only in one.

`test_host_envelope.py` is deliberately **not** copied: it tests the host's
run envelope (`backend/agent_runtime/envelope.py`), which is host-side.

## Why everything here is prefixed with `core_`

The host's suite has `tests/agent_core/loop_helpers.py` and
`tests/agent_core/test_agent.py`, and this directory holds copies of both. In one
pytest process two files with the same module basename are a **collection error**
(`import file mismatch`) and two modules with the same name silently resolve to
whichever was imported first — which would have made these tests import the host's
helper.

So the copies are renamed (`core_loop_helpers.py`, `test_core_agent.py`, …), which
makes `pytest tests/agent_core packages/agent-harness/tests/core` collect both
suites without a clash. The application repository's `pytest.ini` additionally keeps
a bare `pytest` from recursing into `packages/` at all.
