"""Optional: the same durable-edit chain, driven by a *real* model provider.

This file is deliberately **not** in `examples/*.py`: the automated gate runs every
script in that directory, and a script that needs a paid endpoint must never be part
of it. Nothing here runs unless a host asks for it explicitly and supplies the
configuration.

    python examples/optional/real_model_edit.py \
        --state /tmp/yanxu-real-edit \
        --base-url https://example.invalid/v1 \
        --model some-model \
        --task "把欢迎语改得更短" \
        [--api-key-env OPENAI_API_KEY] [--approve]

The API key is read from the environment variable *you name* (`--api-key-env`,
default `OPENAI_API_KEY`) and handed to the provider as a `SecretReference`; it is
never written to a file, a log line or an event. With no key the script says so and
exits 3 — it does not hang waiting for one, and it makes no request.

`--approve` performs the whole chain non-interactively: start (pauses), print the
preview, record the approval, resume. Without it, the script stops after the pause
and prints the commands to continue, so the operator decides in a separate step.

The wiring is the *same* deployment as the default example — it is imported from
`examples/05_durable_approved_edit.py` by path rather than copied, so the thing being
demonstrated cannot drift from the thing the tests and the gate cover.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

from yanxu_harness.providers import CoreStreamBridge, OpenAICompatibleProvider, ProviderConfig, SecretReference
from yanxu_harness.runtime import RunRequest
from yanxu_harness.tools import ApprovalStatus

EXAMPLE = Path(__file__).resolve().parents[1] / "05_durable_approved_edit.py"
MISSING_KEY_EXIT = 3


def load_deployment_module():
    """Import the default example by path and reuse its wiring."""
    spec = importlib.util.spec_from_file_location("yanxu_example_durable_edit", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def real_provider(args: argparse.Namespace, key: str) -> OpenAICompatibleProvider:
    """Build the real provider with configuration the *host* supplied on the command line."""
    config = ProviderConfig(
        model=args.model,
        base_url=args.base_url,
        api_key=SecretReference(args.api_key_env),
        timeout_seconds=args.timeout,
    )
    return OpenAICompatibleProvider(config, secrets={args.api_key_env: key})


async def run_once(args: argparse.Namespace, key: str) -> int:
    example = load_deployment_module()
    state = Path(args.state).expanduser()

    # The provider is the only part that differs from the default example; everything
    # else (gateway, policy, approvals, ledgers, business store) is identical.
    provider = real_provider(args, key)

    deployment = example.Deployment(state, text=args.task, expected_version=1, attempt="a1")
    deployment.harness._stream_fn = CoreStreamBridge(provider)  # noqa: SLF001 - the one seam
    started = await deployment.harness.start(
        RunRequest(prompt=args.task), deployment.context("a1")
    )
    print(json.dumps({"step": "start", **example._outcome_payload(started, deployment.store)}, ensure_ascii=False, default=str))
    if started.status.value != "suspended":
        print(
            f"the run ended as {started.status.value} without asking for an approval; "
            "nothing to approve (the model may not have called the tool)",
            file=sys.stderr,
        )
        return 1

    interaction = started.pending_interaction or {}
    key_to_approve = interaction.get("approval_binding_key")
    preview = interaction.get("preview") or {}
    print(
        "preview: "
        f"{preview.get('path')} version {preview.get('current_version')} "
        f"{preview.get('current_text')!r} → {preview.get('proposed_text')!r}"
    )

    if not args.approve:
        print(
            "not approving (pass --approve to continue). To do it by hand:\n"
            f"  python {EXAMPLE} --state {state} approve --approval {key_to_approve} --actor you\n"
            f"  python {EXAMPLE} --state {state} resume --run {started.run_id}"
        )
        return 0

    approval = await deployment.approvals.decide(
        key_to_approve, ApprovalStatus.APPROVED, decided_by=args.actor
    )
    print(json.dumps({"step": "approve", "status": approval.status.value, "by": approval.decided_by}))
    outcome = await deployment.harness.resume(
        started.run_id, {}, deployment.context("a2")
    )
    print(json.dumps({"step": "resume", **example._outcome_payload(outcome, deployment.store)}, ensure_ascii=False, default=str))
    deployment.store.close()
    deployment.approvals.close()
    return 0 if outcome.status.value == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", required=True, help="directory for the four ledgers")
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible endpoint base URL")
    parser.add_argument("--model", required=True, help="model name to request")
    parser.add_argument("--task", default="把欢迎语改得更短", help="what to ask the model to do")
    parser.add_argument(
        "--api-key-env",
        default="OPENAI_API_KEY",
        help="name of the environment variable holding the key (never a literal)",
    )
    parser.add_argument("--actor", default="local-operator", help="who approves, when --approve is used")
    parser.add_argument("--timeout", type=float, default=120.0, help="provider timeout in seconds")
    parser.add_argument("--approve", action="store_true", help="record the approval and resume in one go")
    args = parser.parse_args(argv)

    key = os.environ.get(args.api_key_env, "")
    if not key:
        print(
            f"{args.api_key_env} is not set, so no request will be made. This script only reads the "
            "key from the environment variable you name; it never contains one.",
            file=sys.stderr,
        )
        return MISSING_KEY_EXIT
    return asyncio.run(run_once(args, key))


if __name__ == "__main__":
    raise SystemExit(main())
