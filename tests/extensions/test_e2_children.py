"""EXT-AC31: a child receives only the explicitly delegated extension subset."""

from __future__ import annotations

import asyncio
import json

import pytest

import test_tasks_children as children
from yanxu_harness.extensions import ExtensionContribution, open_extensions, plan_extensions
from yanxu_harness.tasks import ChildCoordinator, ChildRefused, SpawnRefusal

from ext_support import RecordingFactory, binding, manifest, read_tool, write_tool


def _deployment_facts() -> tuple[tuple[str, ...], str, str]:
    async def scenario():
        factory = RecordingFactory(
            ExtensionContribution(
                tools=(read_tool("reference.read"), write_tool("reference.write"))
            )
        )
        plan = plan_extensions(
            manifests=[
                manifest(
                    tools=("reference.read", "reference.write"),
                    skills=(),
                    capabilities=("tools.v1",),
                )
            ],
            factories={"example.reference": factory},
            bindings=[
                binding(
                    tools=("reference.read", "reference.write"),
                    skills=(),
                    capabilities=("tools.v1",),
                    resource_refs={"credential": "canary-secret-value"},
                )
            ],
        )
        deployment = await open_extensions(plan).open()
        try:
            return (
                deployment.registry.names(),
                deployment.fingerprint,
                json.dumps(deployment.report(), ensure_ascii=False),
            )
        finally:
            await deployment.aclose()

    return asyncio.run(scenario())


def test_a_child_gets_only_the_explicit_extension_tool_subset(tmp_path) -> None:
    names, fingerprint, report = _deployment_facts()
    assert names == ("reference.read", "reference.write")
    assert "canary-secret-value" not in report, "a resource reference never enters a report"

    store = children._store(tmp_path)
    try:
        children._admit(store, "parent", profile="worker")
        policy = children._policy(tools=frozenset(names))
        coordinator = ChildCoordinator(store, policy=policy)
        request = children._request(
            "parent",
            spawn="spawn:subset",
            child="child",
            profile="worker",
            tools=("reference.read",),
            amount=1.0,
        )
        outcome = children.run(coordinator.spawn(request))

        assert outcome.authorization.tools == ("reference.read",)
        assert set(outcome.authorization.tools) < set(names)
        assert outcome.authorization.budget_amount == 1.0
        assert outcome.authorization.budget_cap == 5.0
        assert outcome.authorization.max_depth == 1
        assert outcome.relation.spawn_spec["deadline_at"] is None

        relation = children.run(store.read_child("spawn:subset"))
        rendered = json.dumps(relation.to_record(), ensure_ascii=False, default=str)
        assert "canary-secret-value" not in rendered
        assert "factory" not in rendered
        assert "open(" not in rendered

        child = children.run(store.read_task("child"))
        assert child is not None
        assert child.parent_task_id == "parent"
    finally:
        store.close()


def test_a_child_cannot_widen_the_extension_tool_set_or_its_budget(tmp_path) -> None:
    names, _fingerprint, _report = _deployment_facts()
    store = children._store(tmp_path)
    try:
        children._admit(store, "parent", profile="worker")
        coordinator = ChildCoordinator(
            store, policy=children._policy(tools=frozenset(names))
        )

        with pytest.raises(ChildRefused) as outside:
            children.run(
                coordinator.spawn(
                    children._request(
                        "parent",
                        spawn="spawn:outside",
                        child="outside",
                        profile="worker",
                        tools=("reference.admin",),
                        amount=1.0,
                    )
                )
            )
        assert outside.value.refusal == SpawnRefusal.NOT_PERMITTED.value
        assert children.run(store.read_child("spawn:outside")) is None

        with pytest.raises(ChildRefused) as budget:
            children.run(
                coordinator.spawn(
                    children._request(
                        "parent",
                        spawn="spawn:budget",
                        child="budget",
                        profile="worker",
                        tools=("reference.read",),
                        amount=99.0,
                    )
                )
            )
        assert budget.value.refusal == SpawnRefusal.BUDGET_REFUSED.value
        assert children.run(store.read_child("spawn:budget")) is None
    finally:
        store.close()
