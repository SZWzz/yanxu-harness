"""EXT-AC01/02/04/07: manifest parsing is strict, bounded and pure data."""

from __future__ import annotations

import json

import pytest

from yanxu_harness.extensions import ExtensionLimits, ManifestError, parse_manifest

from ext_support import manifest_payload


def test_a_valid_manifest_becomes_immutable_data() -> None:
    payload = manifest_payload(title="Reference pack", description="A small pack.")
    document = json.dumps(payload, indent=2)

    parsed = parse_manifest(document)

    assert parsed.id == "example.reference"
    assert parsed.version == "1.0.0"
    assert parsed.manifest_schema == 1
    assert parsed.extension_api == 1
    assert parsed.requires_capabilities == ("skills.v1", "tools.v1")
    assert parsed.contributes.tools == ("example.reference.lookup",)
    assert parsed.contributes.skills == ("reference_lookup",)
    assert parsed.title == "Reference pack"


@pytest.mark.parametrize(
    "document",
    [
        '{"manifest_schema":1,"manifest_schema":1}',
        '{"manifest_schema":1,"extension_api":1,"id":"a.b","version":"1",'
        '"requires_capabilities":[],"contributes":{"tools":[],"tools":[]}}',
    ],
)
def test_duplicate_json_keys_are_refused_at_any_depth(document: str) -> None:
    with pytest.raises(ManifestError, match="duplicate"):
        parse_manifest(document)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_are_refused(literal: str) -> None:
    document = manifest_payload()
    document["version"] = literal  # rendered as a bare token below
    text = json.dumps(document).replace(f'"{literal}"', literal)

    with pytest.raises(ManifestError, match="finite"):
        parse_manifest(text)


@pytest.mark.parametrize(
    "field",
    ["authorization", "permissions", "effects", "read_only", "idempotent", "python_path"],
)
def test_a_manifest_cannot_claim_authority(field: str) -> None:
    payload = manifest_payload()
    payload[field] = True

    with pytest.raises(ManifestError, match="unknown"):
        parse_manifest(payload)


def test_unknown_api_generations_are_refused() -> None:
    with pytest.raises(ManifestError, match="manifest_schema"):
        parse_manifest(manifest_payload(manifest_schema=2))
    with pytest.raises(ManifestError, match="extension_api"):
        parse_manifest(manifest_payload(extension_api=2))


def test_unknown_required_capabilities_are_refused() -> None:
    with pytest.raises(ManifestError, match="capability"):
        parse_manifest(manifest_payload(capabilities=("web.v1",)))


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": ""},
        {"id": "Upper.Case"},
        {"id": "x" * 121},
        {"version": ""},
        {"version": "1\n2"},
        {"version": "x" * 200},
        {"requires_capabilities": "tools.v1"},
        {"requires_capabilities": ["tools.v1", "tools.v1"]},
        {"contributes": []},
        {"contributes": {"tools": "example.reference.lookup", "skills": []}},
        {"title": 3},
        {"homepage": "not a single-line bounded string " + "x" * 5000},
    ],
)
def test_malformed_fields_are_refused(overrides: dict) -> None:
    with pytest.raises(ManifestError):
        parse_manifest(manifest_payload(**overrides))


@pytest.mark.parametrize(
    "tools",
    [
        ("Example.Reference.Lookup",),
        ("example.reference.lookup", "example.reference.lookup"),
        ("bad name",),
    ],
)
def test_declared_tool_names_are_stable_ids(tools: tuple[str, ...]) -> None:
    with pytest.raises(ManifestError):
        parse_manifest(manifest_payload(tools=tools))


def test_the_document_size_depth_and_node_limits_are_enforced() -> None:
    with pytest.raises(ManifestError, match="bytes"):
        parse_manifest(
            manifest_payload(description="x" * 400),
            limits=ExtensionLimits(max_manifest_bytes=120),
        )

    nested: dict = {}
    cursor = nested
    for _ in range(20):
        cursor["next"] = {}
        cursor = cursor["next"]
    with pytest.raises(ManifestError, match="depth"):
        parse_manifest(
            manifest_payload(extra=nested),
            limits=ExtensionLimits(max_manifest_depth=4),
        )

    with pytest.raises(ManifestError, match="nodes"):
        parse_manifest(
            manifest_payload(
                requires_capabilities=[
                    "tools.v1",
                    "skills.v1",
                    *[f"extra.v{i}" for i in range(20)],
                ]
            ),
            limits=ExtensionLimits(max_manifest_nodes=12),
        )


def test_mapping_input_is_copied_not_retained() -> None:
    payload = manifest_payload()
    parsed = parse_manifest(payload)

    payload["id"] = "changed.after.parse"
    payload["contributes"]["tools"].append("example.reference.extra")
    payload["requires_capabilities"].append("mcp.tools.v1")

    assert parsed.id == "example.reference"
    assert parsed.contributes.tools == ("example.reference.lookup",)
    assert parsed.requires_capabilities == ("skills.v1", "tools.v1")


def test_only_whitelisted_display_fields_are_accepted() -> None:
    parsed = parse_manifest(
        manifest_payload(title="Reference", description="A pack.", homepage="https://example.test")
    )
    assert parsed.title == "Reference"
    assert parsed.homepage == "https://example.test"

    with pytest.raises(ManifestError, match="unknown"):
        parse_manifest(manifest_payload(icon="not-whitelisted"))
