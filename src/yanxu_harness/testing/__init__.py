"""Adapter contract-test helpers that do not depend on a test framework."""

from .extensions import check_extension_deployment_contract
from .contract_suite import (
    check_persistence_contract,
    check_skill_provider_contract,
    check_unknown_is_terminal,
)
from .fakes import RecordingExecutionBackend, ScriptedProvider, failing_handler

__all__ = [
    "check_extension_deployment_contract",
    "RecordingExecutionBackend",
    "ScriptedProvider",
    "check_persistence_contract",
    "check_skill_provider_contract",
    "check_unknown_is_terminal",
    "failing_handler",
]
