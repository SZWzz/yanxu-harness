"""yanxu-harness: an embeddable Python agent harness.

The public surface is grouped by responsibility and is smaller than the module
tree:

* :mod:`yanxu_harness.errors` — the neutral error taxonomy,
* :mod:`yanxu_harness.versioning` — fingerprints and version refusal,
* :mod:`yanxu_harness.events` — versioned events and the sink/reader ports,
* :mod:`yanxu_harness.runtime` — run identity/authority, lifecycle, turn budgets,
* :mod:`yanxu_harness.tools` — the four tool layers,
* :mod:`yanxu_harness.skills` — the three skill layers,
* :mod:`yanxu_harness.decisions` — the optional decision routers,
* :mod:`yanxu_harness.context` — archive, projection and context budgets,
* :mod:`yanxu_harness.storage` — persistence ports and the volatile reference store,
* :mod:`yanxu_harness.providers` — the provider port and configuration.

Stability for ``0.1.0a1``: the names re-exported here and listed in
``docs/contract.md`` are the *public* API. Everything else — including every
module's private helpers and any attribute whose name starts with an underscore —
is internal and may change between alpha releases. Persisted formats are
versioned regardless of the package version; a reader refuses what it cannot
prove safe instead of guessing.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: The distribution version. Kept in sync with ``pyproject.toml``; the wheel is
#: the authority once installed, so nothing here reads the host's VERSION file.
__version__ = "0.1.0a1"
