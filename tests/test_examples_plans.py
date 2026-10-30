"""The Plan quickstart and crash recovery remain runnable without network access."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

from plan_dag_support import plan_candidate_json, parse_plan_candidate  # noqa: E402


def _run_example(name: str) -> list[dict[str, object]]:
    env = os.environ.copy()
    python_path = [str(ROOT / "src"), str(ROOT / "examples")]
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / name)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def test_offline_plan_dag_example_completes_both_accepted_steps() -> None:
    records = _run_example("15_plan_dag.py")

    assert records[-1]["plan_status"] == "completed"
    assert [item["step_id"] for item in records[-1]["steps"]] == ["inspect", "verify"]
    assert all(item["acceptance"] == "accepted" for item in records[-1]["steps"])
    assert records[-1]["outbox_notices_delivered"] > 0


def test_plan_recovery_example_reopens_original_task_run_and_receipt() -> None:
    records = _run_example("16_plan_recovery.py")

    crash = next(item for item in records if item.get("crash_window"))
    recovered = next(item for item in records if item.get("recovered_binding"))
    assert crash["crash_window"] == "run_committed_before_task_settlement"
    assert recovered["run_id"] == crash["run_id"]
    assert recovered["task_status_after_recovery"] == "completed"
    assert recovered["gateway_receipt"]
    assert records[-1]["plan_status"] == "completed"
    assert len(records[-1]["bindings"]) == 2


def test_execution_binding_reference_preserves_legacy_entries_and_recovers_after_drift_refusal() -> None:
    records = _run_example("17_execution_binding.py")

    independent = next(item for item in records if item.get("entry") == "independent_run")
    ordinary = next(item for item in records if item.get("entry") == "ordinary_task")
    approved = next(
        item for item in records if item.get("entry") == "plan_task" and item.get("stage") == "approved"
    )
    child_exit = next(
        item for item in records if item.get("entry") == "plan_task" and item.get("stage") == "child_exit"
    )
    drift = next(
        item for item in records if item.get("entry") == "plan_task" and item.get("stage") == "drift_refused"
    )
    recovered = records[-1]

    assert independent["status"] == "completed"
    assert independent["execution_binding_adapter"] is False
    assert ordinary["status"] == "completed"
    assert ordinary["execution_binding_adapter"] is False
    assert approved["profile_ref"]["version"] == "A"
    assert approved["tool"] == "notes.read" and approved["effect"] == "read"
    assert approved["workspace_isolation"] == "unsupported"
    assert approved["workspace_isolation_claimed"] is False
    assert child_exit["task_status"] == "waiting"
    assert child_exit["run_status"] == "suspended"
    assert child_exit["workspace_version"] == approved["workspace_ref"]["version"]
    assert child_exit["gateway_receipts"] == 1
    assert drift["reason_code"] == "actual_deployment_mismatch"
    assert drift["new_model_dispatches"] == 0
    assert drift["new_handler_calls"] == 0
    assert recovered["stage"] == "restored_config_recovery"
    assert recovered["task_status"] == "completed"
    assert recovered["run_status"] == "completed"
    assert recovered["plan_status"] == "completed"
    assert recovered["binding_reused"] is True
    assert recovered["gateway_receipts"] == 1
    assert recovered["handler_calls"] == 1


def test_typed_budget_example_covers_committed_scopes_without_external_dispatch() -> None:
    records = _run_example("18_typed_budget_scopes.py")

    reserved = records[0]
    released = records[1]
    owner_kinds = {owner[0] for owner in reserved["covered_owners"]}

    assert reserved["stage"] == "reserved"
    assert owner_kinds == {"plan", "task_root", "task_grant"}
    assert reserved["same_idempotent_reservation"] is True
    assert reserved["reserved_by_scope"] == [1, 1, 1]
    assert reserved["external_dispatch"] == "not_performed"
    assert released["stage"] == "released_before_dispatch"
    assert released["state"] == "released"
    assert released["used_by_scope"] == [0, 0, 0]
    assert released["provider_calls"] == 0


def test_typed_dispatch_example_accounts_model_auxiliary_usage_and_gateway_receipt_replay() -> None:
    records = _run_example("19_typed_budget_dispatch.py")

    primary, late, gateway = records
    assert primary["stage"] == "primary_model"
    assert primary["status"] == "completed"
    assert primary["model_requests_actual"] == 1
    assert (primary["input_tokens_actual"], primary["output_tokens_actual"]) == (24, 4)
    assert late["stage"] == "late_auxiliary_usage"
    assert late["state"] == "settled"
    assert late["same_reservation"] is True
    assert late["usage_reports_are_idempotent"] is True
    assert late["model_requests_actual"] == 2
    assert gateway["stage"] == "gateway_receipt_replay"
    assert gateway["first_status"] == "executed"
    assert gateway["replay_status"] == "replayed"
    assert gateway["handler_calls"] == 1
    assert gateway["dispatches_actual"] == 1


def test_three_step_context_example_verifies_only_current_declared_materials_after_process_recovery() -> None:
    records = _run_example("20_step_context.py")

    crash = next(item for item in records if item.get("crash_window"))
    recovered = next(item for item in records if item.get("recovered_binding"))
    completed = records[-1]
    verify = completed["verify_context"]

    assert crash["crash_window"] == "run_committed_before_task_settlement"
    assert recovered["run_id"] == crash["run_id"]
    assert completed["plan_status"] == "completed"
    assert completed["steps"] == ["read-source", "candidate-change", "verify-candidate"]
    assert completed["provider_dispatches"] == 3
    assert verify["status"] == "prepared"
    assert verify["material_sources"] == ["acceptance", "approved_plan", "host"]
    assert verify["wire_material_kinds"] == [
        "acceptance_criterion",
        "accepted_output",
        "host_fact",
        "required_constraint",
        "step_goal",
    ]
    assert verify["rendered_digest_matches_request"] is True
    assert verify["untrusted_text_stayed_out_of_system"] is True
    assert verify["earlier_step_output_omitted"] is True


def test_candidate_with_unrecognized_authority_fields_is_rejected_before_plan_creation() -> None:
    candidate = json.loads(plan_candidate_json())
    candidate["approval"] = "approved"

    try:
        parse_plan_candidate(json.dumps(candidate))
    except ValueError as error:
        assert "exactly title, goal, and steps" in str(error)
    else:
        raise AssertionError("untrusted authority fields must not enter the Plan definition")
