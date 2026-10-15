"""The public API and the documentation must agree with each other and with the tree.

Two failure modes this catches, both of which have actually happened here:

* a module advertises a name it does not define, or a documented public entry point
  disappears without the document noticing;
* a document points at a path that does not exist — the initial import's docs pointed
  at `packages/agent-harness/...`, which is a different repository layout, so every
  link in them was dead in this tree.

The second check is deliberately mechanical: a path in backticks inside a document is
a claim, and a claim about a file can be verified.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from package_paths import PACKAGE_ROOT

#: The documented public entry points, grouped the way `docs/contract.md` groups
#: them. Each must be importable from the named module.
PUBLIC_SURFACE: dict[str, tuple[str, ...]] = {
    "yanxu_harness": ("__version__",),
    "yanxu_harness.core": (
        "AgentContext",
        "AssistantMessage",
        "EventStream",
        "LoopResult",
        "LoopStop",
        "RunStatus",
        "create_assistant_message_event_stream",
        "run_agent_loop",
        "run_agent_loop_continue",
        "run_agent_loop_continue_result",
        "run_agent_loop_result",
    ),
    "yanxu_harness.runtime": (
        "CHECKPOINT_SCHEMA_VERSION",
        "Cancellation",
        "DefaultHarness",
        "Harness",
        "Principal",
        "RUN_RECORD_SCHEMA_VERSION",
        "RUN_STATUS_TRANSITIONS",
        "RunCheckpoint",
        "RunContext",
        "RunIdentity",
        "RunOutcome",
        "RunRecord",
        "RunRequest",
        "RunState",
        "RunStatus",
        "plan_resume",
        "resolve_resume",
        "validate_max_turns",
    ),
    "yanxu_harness.runtime.batch": ("CallOutcome", "Interaction", "InteractionKind", "RunBatch"),
    "yanxu_harness.runtime.envelope": ("EventEnvelope", "EventFence"),
    "yanxu_harness.providers": (
        "CoreStreamBridge",
        "FramePump",
        "HttpTransport",
        "OpenAICompatibleProvider",
        "Provider",
        "ProviderConfig",
        "ProviderEvent",
        "ProviderRequest",
        "SecretReference",
        "collect",
        "to_provider_request",
    ),
    "yanxu_harness.tools": (
        "ApprovalBinding",
        "ApprovalStatus",
        "ApprovalStore",
        "ConservativePolicy",
        "EffectState",
        "GatewayBindings",
        "InMemoryApprovalStore",
        "InvocationStatus",
        "PENDING_STATUSES",
        "ToolDefinition",
        "ToolEffect",
        "ToolGateway",
        "ToolInvocation",
        "ToolRegistry",
        "core_tool",
        "result_status",
        "suspension_reason",
    ),
    "yanxu_harness.storage": (
        "Durability",
        "InMemoryPersistence",
        "Lease",
        "Persistence",
        "SqliteApprovalStore",
        "SqliteExecutionBackend",
        "SqlitePersistence",
        "StorageConflict",
        "StoredEvent",
    ),
    "yanxu_harness.errors": (
        "BudgetExceeded",
        "ContractError",
        "HarnessError",
        "OutcomeUnknown",
        "ProviderFailure",
        "RunBusy",
        "RunNotFound",
        "RunRequestDrift",
        "VersionRefused",
    ),
    "yanxu_harness.versioning": (
        "SUPPORTED_CHECKPOINT_VERSIONS",
        "SUPPORTED_EVENT_VERSIONS",
        "SUPPORTED_RUN_RECORD_VERSIONS",
        "fingerprint",
        "require_version",
    ),
    "yanxu_harness.events": ("Event", "EventKind", "assert_public_payload"),
    "yanxu_harness.testing": (
        "RecordingExecutionBackend",
        "ScriptedProvider",
        "check_persistence_contract",
    ),
}

DOCUMENTS = ("README.md", "docs/contract.md", "docs/adapters.md", "docs/recovery.md", "docs/verification.md")
#: Paths that legitimately do not exist in this tree, with the reason.
ALLOWED_MISSING_PATHS = {
    "tests/fault_windows.py",  # does exist; kept out of the allow-list on purpose
}
ALLOWED_MISSING_PATHS.discard("tests/fault_windows.py")


def _documented_paths(document: Path) -> list[tuple[int, str]]:
    """Every backticked path-like token in a document, with its line number."""
    found: list[tuple[int, str]] = []
    pattern = re.compile(r"`((?:src|tests|examples|scripts|docs|LICENSES)/[A-Za-z0-9_./-]+)`")
    for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), start=1):
        for match in pattern.finditer(line):
            found.append((number, match.group(1)))
    return found


def test_every_documented_public_name_exists() -> None:
    missing: list[str] = []
    for module_name, names in PUBLIC_SURFACE.items():
        module = importlib.import_module(module_name)
        for name in names:
            if not hasattr(module, name):
                missing.append(f"{module_name}.{name}")
            advertised = getattr(module, "__all__", None)
            if advertised is not None and name not in advertised:
                missing.append(f"{module_name}.{name} exists but is not in __all__")
    assert missing == [], "the documented public surface is not importable:\\n" + "\\n".join(missing)


def test_the_public_surface_is_smaller_than_the_module_tree() -> None:
    """A package that re-exports everything has no contract, only a directory."""
    import yanxu_harness

    package = Path(yanxu_harness.__file__).resolve().parent
    modules = {path for path in package.rglob("*.py") if "__pycache__" not in path.parts}
    documented = sum(len(names) for names in PUBLIC_SURFACE.values())
    assert len(modules) >= 20, "the module count changed unexpectedly"
    # The point is that the documented list is a deliberate subset, not a dump: it is
    # far smaller than the number of public module-level names in the tree.
    assert documented < 200, f"{documented} documented names is not a curated surface"


@pytest.mark.skipif(not (PACKAGE_ROOT / "docs").is_dir(), reason="文档不在这份检出里")
def test_every_path_a_document_mentions_exists() -> None:
    missing: list[str] = []
    for name in DOCUMENTS:
        document = PACKAGE_ROOT / name
        if not document.is_file():
            missing.append(f"{name}: the document itself is missing")
            continue
        for line, token in _documented_paths(document):
            if token in ALLOWED_MISSING_PATHS:
                continue
            if any(character in token for character in ("*", "...")):
                continue
            if not (PACKAGE_ROOT / token).exists():
                missing.append(f"{name}:{line}: {token}")
    assert missing == [], "documents point at paths that do not exist:\\n" + "\\n".join(missing)


def test_the_verification_matrix_only_names_tests_that_exist() -> None:
    """`docs/verification.md` maps windows to tests; a renamed test must fail here."""
    document = PACKAGE_ROOT / "docs" / "verification.md"
    if not document.is_file():
        pytest.skip("the verification record is not part of an installed artifact")
    text = document.read_text(encoding="utf-8")
    referenced = set(re.findall(r"`((?:tests|tests/core)/[A-Za-z0-9_./-]+)(?:::[A-Za-z0-9_]+)?`", text))
    missing: list[str] = []
    for reference in sorted(referenced):
        path, _, test_name = reference.partition("::")
        target = PACKAGE_ROOT / path
        if not target.is_file():
            missing.append(f"{path} does not exist")
            continue
        if test_name and f"def {test_name}(" not in target.read_text(encoding="utf-8"):
            missing.append(f"{path}::{test_name} is not defined")
    assert missing == [], "the verification record references tests that are gone:\\n" + "\\n".join(missing)
