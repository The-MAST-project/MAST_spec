"""Test bootstrap: run MAST_common's, from the sibling clone, rather than a copy of it.

It installs, at import and so before collection:

- the macOS shim for `Filer`, without which `import common.utils` -- and so anything that
  imports `common.models` -- fails on a developer Mac;
- the guard that fails any test starting an external process. This repo needs it more than
  most: importing its drivers commands hardware (MAST_spec#77).

`<top>/common` is the flat layout every MAST machine and dev box has.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_COMMON_CONFTEST = Path(__file__).resolve().parents[2] / "common" / "tests" / "conftest.py"

_spec = importlib.util.spec_from_file_location("_mast_common_conftest", _COMMON_CONFTEST)
assert _spec is not None and _spec.loader is not None, f"MAST_common's conftest not found at {_COMMON_CONFTEST}"
_spec.loader.exec_module(importlib.util.module_from_spec(_spec))
