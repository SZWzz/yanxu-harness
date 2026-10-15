"""Make the src-layout package importable when running from a checkout.

The package's own tests must run with nothing but the standard library and
pytest; requiring an editable install first would hide the case the import fence
is meant to catch. When the tests have been copied *out* of the checkout (the
release verifier does that), the installed package is already importable and
nothing is inserted.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from package_paths import ensure_importable  # noqa: E402

ensure_importable()
