from __future__ import annotations

import pytest

from yanxu_harness.core.execution import arrangement_to_record
from yanxu_harness.errors import ResumeRefused
from yanxu_harness.runtime.context import RunIdentity
from yanxu_harness.runtime.default_harness import DefaultHarness, _compare_arrangement


class _BoundRuntime:
    class _Binding:
        digest = "sha256:" + "a" * 64

    binding = _Binding()

    async def check_model_dispatch(self) -> None:
        return None

    async def check_tool_dispatch(self) -> None:
        return None


def test_run_arrangement_persists_and_compares_the_plan_execution_binding() -> None:
    harness = DefaultHarness(stream_fn=lambda *_args: None)
    identity = RunIdentity("ns", "session", "run", "attempt")
    runtime = _BoundRuntime()
    config = harness._make_loop_config(
        {}, identity, max_turns=1, execution_binding=runtime
    )

    recorded = arrangement_to_record(config.arrangement)
    assert recorded["plan_execution_binding"] == runtime.binding.digest

    current = {
        **harness.fingerprints(),
        "plan_execution_binding": "sha256:" + "b" * 64,
    }
    with pytest.raises(ResumeRefused) as refused:
        _compare_arrangement(identity, recorded, current, harness.exposure_manifest())

    assert refused.value.reason_code == "arrangement_drift"
    assert "plan_execution_binding" in refused.value.drifted
