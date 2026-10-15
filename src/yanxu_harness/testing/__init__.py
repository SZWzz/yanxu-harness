"""Adapter contract-test helpers that do not depend on a test framework."""

from .contract_suite import (
    check_persistence_contract,
    check_skill_provider_contract,
    check_unknown_is_terminal,
)
from .fakes import RecordingExecutionBackend, ScriptedProvider, failing_handler

__all__ = [
    "RecordingExecutionBackend",
    "ScriptedProvider",
    "check_persistence_contract",
    "check_skill_provider_contract",
    "check_unknown_is_terminal",
    "failing_handler",
]
