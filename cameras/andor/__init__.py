"""Andor camera support.

Makes the vendored SDK reachable, in the two ways it needs: the Python package
on `sys.path`, and the native DLL on `PATH`. Both are here rather than in
newton.py so there is one place that answers "how is the vendored SDK wired up".

**The package** has to be imported by its own top-level name. Its `__init__.py`
self-imports absolutely -- `from pyAndorSDK2._version import ...` -- so reaching
it through this repo's package path (`cameras.andor.sdk.pyAndorSDK2.pyAndorSDK2`)
raises ModuleNotFoundError from inside the vendor's own file, even though the
directory sits right there. Only `<repo>/cameras/andor/sdk/pyAndorSDK2` on
sys.path makes the vendor's assumption true.

Nothing else supplies it: pyAndorSDK2 is not on PyPI, is not installed in the
venv, and the venv's mast.pth adds only <top>. That left `import pyAndorSDK2`
working solely under an IDE that injects source roots, and failing whenever spec
was started any other way.

**The DLL** needs the platform subdirectory, not the `libs/` root the vendor's
own `__init__.py` prepends. That line assumes the *installed* layout: setup.py
copies `libs/Windows/<bits>/*` flat into `site-packages/pyAndorSDK2/libs/` as a
post-install step. We run the package in place, where the DLL is still at
`libs/Windows/64/atmcd64d.dll`, so `ctypes.util.find_library("atmcd64d.dll")`
-- which just scans PATH directories for the file -- returned None and
`windll.LoadLibrary(None)` raised `TypeError: argument of type 'NoneType' is not
iterable` from `atmcd.__init__`.

The 32/64 choice mirrors `atmcd._load_library` exactly, so this cannot pick a
directory whose DLL name that function is not looking for. Linux is left alone:
there the vendor loads `libandor.so` through the system loader.
"""

import os
import platform
import sys
from pathlib import Path

_sdk_root = Path(__file__).parent / "sdk" / "pyAndorSDK2"

if str(_sdk_root) not in sys.path:
    sys.path.insert(0, str(_sdk_root))

if sys.platform == "win32":
    _bits = "64" if platform.machine() == "AMD64" else "32"
    _dll_dir = str(_sdk_root / "pyAndorSDK2" / "libs" / "Windows" / _bits)
    if _dll_dir not in os.environ["PATH"].split(os.pathsep):
        os.environ["PATH"] = _dll_dir + os.pathsep + os.environ["PATH"]
