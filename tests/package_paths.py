"""Where the package's own source lives, whether or not this is a checkout.

The package's tests are shipped in the sdist and copied out of the tree by
``scripts/verify_release.py``; from there there is no ``../src`` to point at. So
resolve the source root in one place:

* if this file is inside a checkout, that is the checkout's ``src``;
* otherwise it is the installed package's own parent directory.

Tests that verify a *checkout-only* artifact (the dependency declaration in
``pyproject.toml``) skip with an explicit reason instead of failing, because a
missing ``pyproject.toml`` is not a defect in an installed wheel.
"""

from __future__ import annotations

import sys
from pathlib import Path


def source_root() -> Path:
    checkout = Path(__file__).resolve().parents[1] / "src"
    if (checkout / "yanxu_harness").is_dir():
        return checkout
    import yanxu_harness

    return Path(yanxu_harness.__file__).resolve().parent.parent


def is_checkout() -> bool:
    return (source_root().parent / "pyproject.toml").is_file()


def ensure_importable() -> Path:
    root = source_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


PACKAGE_ROOT = source_root().parent
