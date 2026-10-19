"""Make the MCP test support package importable as `support.*`.

The suite is copied out of the checkout by the release verifier, so it may not
rely on `tests/` being a package; it only ever needs its own directory.

Note: a directory named `mcp` inside `tests/` is itself a namespace package, so
`import mcp` succeeds even with no SDK installed. Nothing in this directory
imports the SDK at module scope; the test modules ask
`support.harness.sdk_available()` first, and the MCP gate refuses to count a
skipped suite as a pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
