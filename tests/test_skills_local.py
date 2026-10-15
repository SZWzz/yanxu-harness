"""The local SKILL.md provider: bounds, confinement, and what a skill cannot do.

Reading front matter needs the optional ``yaml`` extra. When it is missing, the
tests that exercise a *successfully parsed* skill skip with that exact reason —
the provider itself reports a ``ConfigurationError`` naming the extra, which
:func:`test_a_missing_yaml_extra_is_reported_with_the_extra_name` checks without
needing the extra to be absent. The release verifier installs the extra so the
real checks run there.
"""

from __future__ import annotations

import asyncio

import pytest

def _yaml_available() -> bool:
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


needs_yaml = pytest.mark.skipif(
    not _yaml_available(),
    reason="needs the optional 'yaml' extra (pip install 'yanxu-harness[yaml]')",
)

from yanxu_harness.errors import ConfigurationError, ContractError
from yanxu_harness.skills import (
    LocalSkillProvider,
    SkillBudgetExceeded,
    SkillLimits,
    SkillNotFound,
    SkillResourceDenied,
)
from yanxu_harness.testing import check_skill_provider_contract


def run(coro):
    return asyncio.run(coro)


def write_skill(
    root,
    skill_id: str,
    *,
    body: str = "How to do the thing.",
    version: str = "1",
    extra_front: str = "",
    resources: dict[str, str] | None = None,
    front_matter: str | None = None,
) -> None:
    directory = root / skill_id
    (directory / "resources").mkdir(parents=True, exist_ok=True)
    front = front_matter if front_matter is not None else (
        f"id: {skill_id}\n"
        f"name: {skill_id.title()}\n"
        f"description: the {skill_id} method\n"
        f"version: {version}\n"
        "when_to_use: the task matches\n"
        "avoid_when: it does not\n"
        "tools: [notes.read]\n"
        f"{extra_front}"
    )
    (directory / "SKILL.md").write_text(f"---\n{front}---\n{body}\n", encoding="utf-8")
    for name, text in (resources or {}).items():
        target = directory / "resources" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def provider(root, **kwargs) -> LocalSkillProvider:
    return LocalSkillProvider(roots=[root], **kwargs)


# -- listing and loading ------------------------------------------------


@needs_yaml
def test_listing_reads_front_matter_without_loading_bodies(tmp_path) -> None:
    write_skill(tmp_path, "outline", body="x" * 5000)
    listing = run(provider(tmp_path).list_metadata())
    assert [item.skill_id for item in listing] == ["outline"]
    assert listing[0].tools == ("notes.read",)
    assert listing[0].fingerprint.startswith("sha256:")


@needs_yaml
def test_a_body_is_loaded_on_demand_with_its_resources_listed(tmp_path) -> None:
    write_skill(tmp_path, "outline", resources={"templates/basic.md": "# Outline\n"})
    body = run(provider(tmp_path).load_body("outline"))
    assert body.text.strip() == "How to do the thing."
    assert body.resources == ("templates/basic.md",)
    assert body.scope == "global"


def test_a_resource_is_read_on_demand(tmp_path) -> None:
    write_skill(tmp_path, "outline", resources={"templates/basic.md": "# Outline\n"})
    resource = run(provider(tmp_path).read_resource("outline", "templates/basic.md"))
    assert resource.text == "# Outline\n"
    assert resource.byte_length == len("# Outline\n".encode("utf-8"))


@needs_yaml
def test_the_fingerprint_tracks_content(tmp_path) -> None:
    write_skill(tmp_path, "outline")
    first = run(provider(tmp_path).fingerprint("outline"))
    write_skill(tmp_path, "outline", body="A different method.")
    assert run(provider(tmp_path).fingerprint("outline")) != first


def test_an_unknown_skill_is_reported_not_invented(tmp_path) -> None:
    write_skill(tmp_path, "outline")
    with pytest.raises(SkillNotFound):
        run(provider(tmp_path).load_body("missing"))


@needs_yaml
def test_a_directory_without_a_skill_file_is_not_a_skill(tmp_path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "readme.txt").write_text("not a skill", encoding="utf-8")
    write_skill(tmp_path, "outline")
    listing = run(provider(tmp_path).list_metadata())
    assert [item.skill_id for item in listing] == ["outline"]


@needs_yaml
def test_only_the_given_roots_are_scanned(tmp_path) -> None:
    visible = tmp_path / "visible"
    hidden = tmp_path / "hidden"
    visible.mkdir()
    hidden.mkdir()
    write_skill(visible, "outline")
    write_skill(hidden, "secret")
    listing = run(LocalSkillProvider(roots=[visible]).list_metadata())
    assert [item.skill_id for item in listing] == ["outline"]
    with pytest.raises(SkillNotFound):
        run(LocalSkillProvider(roots=[visible]).load_body("secret"))


# -- confinement and bounds --------------------------------------------


@pytest.mark.parametrize("skill_id", ["../outside", "a/b", ".hidden", ""])
def test_a_skill_id_cannot_be_a_path(tmp_path, skill_id: str) -> None:
    write_skill(tmp_path, "outline")
    with pytest.raises((SkillResourceDenied, SkillNotFound)):
        run(provider(tmp_path).load_body(skill_id))


@pytest.mark.parametrize("path", ["../outside.md", "/etc/passwd", "a/../../b"])
def test_a_resource_path_cannot_escape(tmp_path, path: str) -> None:
    write_skill(tmp_path, "outline")
    with pytest.raises(SkillResourceDenied):
        run(provider(tmp_path).read_resource("outline", path))


def test_a_symlink_out_of_the_resource_directory_is_refused(tmp_path) -> None:
    write_skill(tmp_path, "outline")
    outside = tmp_path / "secret.txt"
    outside.write_text("private", encoding="utf-8")
    link = tmp_path / "outline" / "resources" / "leak.md"
    link.symlink_to(outside)
    with pytest.raises((SkillResourceDenied, SkillNotFound)):
        run(provider(tmp_path).read_resource("outline", "leak.md"))


@needs_yaml
def test_an_oversized_body_is_refused(tmp_path) -> None:
    write_skill(tmp_path, "outline", body="x" * 100)
    with pytest.raises(SkillBudgetExceeded):
        run(provider(tmp_path, limits=SkillLimits(body_chars=50)).load_body("outline"))


def test_an_oversized_resource_is_refused(tmp_path) -> None:
    write_skill(tmp_path, "outline", resources={"big.md": "x" * 100})
    with pytest.raises(SkillBudgetExceeded):
        run(provider(tmp_path, limits=SkillLimits(resource_bytes=10)).read_resource("outline", "big.md"))


@needs_yaml
def test_too_many_resources_are_refused(tmp_path) -> None:
    write_skill(tmp_path, "outline", resources={"a.md": "a", "b.md": "b"})
    with pytest.raises(SkillBudgetExceeded):
        run(provider(tmp_path, limits=SkillLimits(max_resources=1)).load_body("outline"))


# -- front matter -------------------------------------------------------


def test_missing_front_matter_is_refused_with_a_reason(tmp_path) -> None:
    directory = tmp_path / "outline"
    directory.mkdir()
    (directory / "SKILL.md").write_text("no front matter here", encoding="utf-8")
    with pytest.raises(ContractError, match="front matter"):
        run(provider(tmp_path).load_body("outline"))


def test_unclosed_front_matter_is_refused(tmp_path) -> None:
    directory = tmp_path / "outline"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\nid: outline\nbody without a close", encoding="utf-8")
    with pytest.raises(ContractError, match="not closed"):
        run(provider(tmp_path).load_body("outline"))


@needs_yaml
def test_a_non_mapping_front_matter_is_refused(tmp_path) -> None:
    write_skill(tmp_path, "outline", front_matter="- just\n- a list\n")
    with pytest.raises(ContractError, match="must be a mapping"):
        run(provider(tmp_path).load_body("outline"))


@needs_yaml
def test_missing_optional_front_matter_fields_are_tolerated(tmp_path) -> None:
    write_skill(tmp_path, "outline", front_matter="description: bare minimum\n")
    body = run(provider(tmp_path).load_body("outline"))
    assert body.version == "1"
    assert body.metadata["when_to_use"] == ""


# -- the shared adapter contract ---------------------------------------


@needs_yaml
def test_the_local_provider_passes_the_shared_adapter_contract(tmp_path) -> None:
    write_skill(tmp_path, "outline", resources={"templates/basic.md": "# Outline\n"})
    observed = run(
        check_skill_provider_contract(provider(tmp_path), skill_id="outline", scope="global")
    )
    assert set(observed) >= {"metadata-bounded", "body-on-demand", "path-confined", "fingerprint"}


# -- what a skill cannot do --------------------------------------------


@needs_yaml
def test_a_skill_file_cannot_grant_anything(tmp_path) -> None:
    """Whatever a skill says about tools, the provider returns it as inert metadata."""
    write_skill(
        tmp_path,
        "outline",
        extra_front="allowed_tools: [run_bash, files.write]\npermissions: [admin]\n",
    )
    listing = run(provider(tmp_path).list_metadata())
    metadata = listing[0]
    # Only the declared `tools` hint survives, as plain names, and there is no
    # permission-shaped field anywhere on the metadata.
    assert metadata.tools == ("notes.read",)
    assert "allowed_tools" not in metadata.__dict__
    assert not hasattr(metadata, "permissions")
    assert not hasattr(metadata, "execute")
    assert not hasattr(metadata, "approve")


def test_a_root_that_does_not_exist_yields_nothing_instead_of_failing(tmp_path) -> None:
    assert run(LocalSkillProvider(roots=[tmp_path / "absent"]).list_metadata()) == ()


def test_a_provider_without_roots_is_refused() -> None:
    with pytest.raises(ContractError):
        LocalSkillProvider(roots=[])


@pytest.mark.skipif(_yaml_available(), reason="only meaningful when the extra is absent")
def test_a_missing_yaml_extra_is_reported_with_the_extra_name(tmp_path) -> None:
    """A missing optional dependency must name itself, not fail with an ImportError."""
    directory = tmp_path / "outline"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\nid: outline\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as excinfo:
        run(LocalSkillProvider(roots=[tmp_path]).load_body("outline"))
    message = str(excinfo.value)
    assert "yaml" in message and "extra" in message
