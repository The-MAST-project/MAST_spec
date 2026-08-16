"""Andor camera support.

Puts the vendored SDK root on `sys.path` so `import pyAndorSDK2` resolves.

The wrapper has to be imported by its own top-level name. Its `__init__.py`
self-imports absolutely -- `from pyAndorSDK2._version import ...` -- so reaching
it through this repo's package path (`cameras.andor.sdk.pyAndorSDK2.pyAndorSDK2`)
raises ModuleNotFoundError from inside the vendor's own file, even though the
directory sits right there. Only `<repo>/cameras/andor/sdk/pyAndorSDK2` on
sys.path makes the vendor's assumption true.

Nothing else supplies it: pyAndorSDK2 is not on PyPI, is not installed in the
venv, and the venv's mast.pth adds only <top>. That left `import pyAndorSDK2`
working solely under an IDE that injects source roots into sys.path, and failing
whenever spec was started any other way.

The DLLs need no further help -- the vendor's `__init__.py` prepends its own
`libs/` to PATH, and they are vendored alongside it.
"""

import sys
from pathlib import Path

_sdk_root = str(Path(__file__).parent / "sdk" / "pyAndorSDK2")
if _sdk_root not in sys.path:
    sys.path.insert(0, _sdk_root)
