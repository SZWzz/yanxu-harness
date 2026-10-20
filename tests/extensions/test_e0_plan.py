"""EXT-AC02/03/05/06/07: planning is pure, deterministic and additive-only."""

from __future__ import annotations

import json
import socket

import pytest

from yanxu_harness.extensions import (
    ExtensionBinding,
    ExtensionContribution,
    ExtensionLimits,
    ExtensionManifest,
    ExtensionRefused,
    ExtensionConflict,
    ExtensionLimitExceeded,
    SkillContribution,
    describe_plan,
    parse_manifest,
    plan_extensions,
)
from yanxu_harness.tools.registry import ToolRegistry

from ext_support import (
    RecordingFactory,
    binding,
    manifest,
    manifest_payload,
    read_tool,
    skill_provider,
)


def _plan(*, manifests=(), factories=None, bindings=(), **kwargs):
    return plan_extensions(
        manifests=manifests,
        factories=factories or {},
        bindings=bindings,
        **kwargs,
    )


def test_planning_calls_no_factory_and_keeps_the_host_registry_unchanged() -> None:
    factory = RecordingFactory(
        contribution=lambda: (_ for _ in ()).throw(AssertionError("factory was opened"))
    )
    local = read_tool("host.local.read", description="A host tool.")
    host_registry = ToolRegistry([local])

    def forbidden(*args, **kwargs):
        raise AssertionError("the pure precheck performed I/O")

    original_connect = socket.socket.connect
    socket.socket.connect = forbidden  # type: ignore[method-assign]
    try:
        plan = _plan(
            manifests=[manifest()],
            factories={"example.reference": factory},
            bindings=[binding()],
            local_tools=host_registry.definitions(),
        )
        described = describe_plan(plan)
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]

    assert factory.opened == 0
    assert factory.contribution_calls == 0
    assert plan.fingerprint.startswith("sha256:")
    assert described["extensions"][0]["id"] == "example.reference"
    assert tuple(host_registry.names()) == ("host.local.read",)
    assert len(host_registry) == 1


def test_the_same_extension_id_is_refused_even_with_the_same_version() -> None:
    with pytest.raises(ExtensionConflict, match="duplicate"):
        _plan(
            manifests=[
                parse_manifest(manifest_payload()),
                parse_manifest(manifest_payload()),
            ],
            factories={"example.reference": RecordingFactory()},
            bindings=[binding()],
        )


def test_a_missing_factory_or_binding_is_refused() -> None:
    with pytest.raises(ExtensionRefused, match="factory"):
        _plan(manifests=[manifest()], factories={}, bindings=[binding()])
    with pytest.raises(ExtensionRefused, match="binding"):
        _plan(
            manifests=[manifest()],
            factories={"example.reference": RecordingFactory()},
            bindings=[],
        )


def test_a_binding_cannot_grant_authority_the_manifest_did_not_require() -> None:
    with pytest.raises(ExtensionRefused, match="capabilit"):
        _plan(
            manifests=[manifest(capabilities=("tools.v1",))],
            factories={"example.reference": RecordingFactory()},
            bindings=[binding(capabilities=("skills.v1", "tools.v1"))],
        )


def test_a_manifest_requirement_must_be_granted() -> None:
    with pytest.raises(ExtensionRefused, match="capabilit"):
        _plan(
            manifests=[manifest(capabilities=("skills.v1", "tools.v1"))],
            factories={"example.reference": RecordingFactory()},
            bindings=[binding(capabilities=("tools.v1",))],
        )


def test_a_bound_contribution_must_be_declared_by_the_manifest() -> None:
    with pytest.raises(ExtensionRefused, match="declare"):
        _plan(
            manifests=[manifest()],
            factories={"example.reference": RecordingFactory()},
            bindings=[binding(tools=("example.reference.other",))],
        )


def test_a_declared_contribution_requires_its_capability() -> None:
    with pytest.raises(ExtensionRefused, match="tools.v1"):
        _plan(
            manifests=[manifest(capabilities=("skills.v1",))],
            factories={"example.reference": RecordingFactory()},
            bindings=[binding(capabilities=("skills.v1",))],
        )


def test_a_tool_name_may_not_be_shared_with_a_host_or_another_extension() -> None:
    with pytest.raises(ExtensionConflict, match="example.reference.lookup"):
        _plan(
            manifests=[manifest()],
            factories={"example.reference": RecordingFactory()},
            bindings=[binding()],
            local_tools=[read_tool()],
        )

    other = manifest(
        extension_id="example.other",
        tools=("example.reference.lookup",),
        skills=(),
        capabilities=("tools.v1",),
    )
    with pytest.raises(ExtensionConflict, match="example.reference.lookup"):
        _plan(
            manifests=[manifest(capabilities=("tools.v1",), skills=()), other],
            factories={
                "example.reference": RecordingFactory(),
                "example.other": RecordingFactory(),
            },
            bindings=[
                binding(skills=(), capabilities=("tools.v1",)),
                binding(
                    extension_id="example.other",
                    tools=("example.reference.lookup",),
                    skills=(),
                    capabilities=("tools.v1",),
                ),
            ],
        )


def test_a_skill_key_may_not_be_shared_with_a_host_or_another_extension() -> None:
    host_skill = SkillContribution(
        provider=skill_provider(),
        skill_ids=("reference_lookup",),
        scope="project-a",
    )
    with pytest.raises(ExtensionConflict, match="reference_lookup"):
        _plan(
            manifests=[manifest()],
            factories={"example.reference": RecordingFactory()},
            bindings=[binding()],
            host_skills=[host_skill],
        )


def test_all_extensions_in_one_deployment_share_one_scope() -> None:
    other = manifest(
        extension_id="example.other",
        tools=("example.other.read",),
        skills=(),
        capabilities=("tools.v1",),
    )
    with pytest.raises(ExtensionRefused, match="scope"):
        _plan(
            manifests=[manifest(), other],
            factories={
                "example.reference": RecordingFactory(),
                "example.other": RecordingFactory(),
            },
            bindings=[
                binding(),
                binding(
                    extension_id="example.other",
                    namespace="project-b",
                    tools=("example.other.read",),
                    skills=(),
                    capabilities=("tools.v1",),
                ),
            ],
        )


def test_aggregate_limits_cannot_be_bypassed_by_per_item_slices() -> None:
    other = manifest(
        extension_id="example.other",
        tools=("example.other.read",),
        skills=(),
        capabilities=("tools.v1",),
    )
    with pytest.raises(ExtensionLimitExceeded, match="extensions"):
        _plan(
            manifests=[manifest(), other],
            factories={
                "example.reference": RecordingFactory(),
                "example.other": RecordingFactory(),
            },
            bindings=[
                binding(),
                binding(
                    extension_id="example.other",
                    tools=("example.other.read",),
                    skills=(),
                    capabilities=("tools.v1",),
                ),
            ],
            limits=ExtensionLimits(max_extensions=1),
        )
    with pytest.raises(ExtensionLimitExceeded, match="tools"):
        _plan(
            manifests=[manifest()],
            factories={"example.reference": RecordingFactory()},
            bindings=[binding()],
            local_tools=[read_tool("host.one"), read_tool("host.two")],
            limits=ExtensionLimits(max_tools=1),
        )
    two_skills = manifest(skills=("reference_lookup", "reference_extra"))
    with pytest.raises(ExtensionLimitExceeded, match="skills"):
        _plan(
            manifests=[two_skills],
            factories={"example.reference": RecordingFactory()},
            bindings=[
                binding(skills=("reference_lookup", "reference_extra"), tools=("example.reference.lookup",))
            ],
            limits=ExtensionLimits(max_skills=1),
        )


def test_local_tools_are_checked_for_duplicates_and_cycles() -> None:
    duplicate = read_tool("host.local.read")
    with pytest.raises(Exception, match="already registered"):
        _plan(manifests=[], factories={}, bindings=[], local_tools=[duplicate, duplicate])

    first = read_tool("host.one", requires=("host.two",))
    second = read_tool("host.two", requires=("host.one",))
    with pytest.raises(Exception, match="cycle"):
        _plan(manifests=[], factories={}, bindings=[], local_tools=[first, second])


def test_plan_identity_ignores_mapping_and_set_order() -> None:
    first = manifest_payload()
    second = {
        "contributes": {"skills": ["reference_lookup"], "tools": ["example.reference.lookup"]},
        "requires_capabilities": ["tools.v1", "skills.v1"],
        "version": "1.0.0",
        "id": "example.reference",
        "extension_api": 1,
        "manifest_schema": 1,
    }
    left = _plan(
        manifests=[parse_manifest(first)],
        factories={"example.reference": RecordingFactory()},
        bindings=[binding()],
    )
    right = _plan(
        manifests=[parse_manifest(second)],
        factories={"example.reference": RecordingFactory()},
        bindings=[binding()],
    )

    assert left.fingerprint == right.fingerprint
    assert describe_plan(left) == describe_plan(right)


def test_plan_identity_preserves_declaration_order_and_version() -> None:
    two_tools = manifest_payload(
        tools=("example.reference.one", "example.reference.two"),
        skills=(),
        capabilities=("tools.v1",),
    )
    reversed_tools = manifest_payload(
        tools=("example.reference.two", "example.reference.one"),
        skills=(),
        capabilities=("tools.v1",),
    )
    left = _plan(
        manifests=[parse_manifest(two_tools)],
        factories={"example.reference": RecordingFactory()},
        bindings=[
            binding(
                tools=("example.reference.one", "example.reference.two"),
                skills=(),
                capabilities=("tools.v1",),
            )
        ],
    )
    right = _plan(
        manifests=[parse_manifest(reversed_tools)],
        factories={"example.reference": RecordingFactory()},
        bindings=[
            binding(
                tools=("example.reference.two", "example.reference.one"),
                skills=(),
                capabilities=("tools.v1",),
            )
        ],
    )
    changed_version = _plan(
        manifests=[parse_manifest(manifest_payload(version="2.0.0"))],
        factories={"example.reference": RecordingFactory()},
        bindings=[binding()],
    )

    assert left.fingerprint != right.fingerprint
    assert left.fingerprint != changed_version.fingerprint


def test_plan_identity_changes_when_binding_revisions_change() -> None:
    base = _plan(
        manifests=[manifest()],
        factories={"example.reference": RecordingFactory()},
        bindings=[binding()],
    )
    changed = _plan(
        manifests=[manifest()],
        factories={"example.reference": RecordingFactory()},
        bindings=[binding(config_revision="cfg-2")],
    )
    assert base.fingerprint != changed.fingerprint


def test_a_factory_outside_the_manifests_is_not_even_asked_for_a_method() -> None:
    class Untouched:
        @property
        def open(self):  # pragma: no cover - the test fails if it is read
            raise AssertionError("an unrelated factory was inspected")

    _plan(
        manifests=[manifest()],
        factories={
            "example.reference": RecordingFactory(),
            "unrelated.ext": Untouched(),
        },
        bindings=[binding()],
    )


def test_the_public_description_contains_no_secret_or_factory_repr() -> None:
    factory = RecordingFactory()
    plan = _plan(
        manifests=[manifest()],
        factories={"example.reference": factory},
        bindings=[binding(resource_refs={"credential": "super-secret-value"})],
    )
    rendered = json.dumps(describe_plan(plan), ensure_ascii=False)

    assert "super-secret-value" not in rendered
    assert repr(factory) not in rendered
    assert "handler" not in rendered


def test_the_report_limit_is_enforced_instead_of_truncating() -> None:
    plan = _plan(
        manifests=[manifest()],
        factories={"example.reference": RecordingFactory()},
        bindings=[binding()],
        limits=ExtensionLimits(max_report_bytes=80),
    )
    with pytest.raises(ExtensionLimitExceeded, match="report"):
        describe_plan(plan)
