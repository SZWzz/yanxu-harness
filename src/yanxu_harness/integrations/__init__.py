"""Optional host integrations.

Nothing here is imported by the base package's own import path: a host opts in by
importing this package explicitly, and even then the third-party SDK behind an
integration stays unloaded until the integration is actually used.
"""

from __future__ import annotations

__all__: list[str] = []
