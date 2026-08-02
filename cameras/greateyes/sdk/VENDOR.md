# Vendored greateyes SDK

Provenance of everything under `cameras/greateyes/sdk/`. Update this file whenever
an SDK component is bumped.

## Python wrapper — `greateyesSDK.py`

| | |
|---|---|
| Version | 22.5 rev2 |
| Upstream file date | 2025-01-29 |
| Source archive | `greateyes_sdk_python_22.5 rev2.zip` |
| Archive sha256 | `f1c05f011c7d912305634922f557e08ac1080457f11727efb13ddfca8c60e927` |
| File sha256 | `3e65b169b0989153bd156950cba0d60016a472b9f6bff66c966b982979f1812d` |
| Retrieved | 2026-08-02 |

Upstream's own changelog for rev2 (from the file header), plus what we observed diffing
it against the previous drop:

- `UpdateStatus` → `UpdateStatus()` in `StartMeasurement_DynBitDepth`,
  `GetMeasurementData_DynBitDepth` and `PerformMeasurement_Blocking_DynBitDepth`. The
  missing parentheses meant the module-level `Status` / `StatusMSG` were never refreshed,
  so every error message quoting them reported stale values.
- `sys.exit()` on an unexpected bit depth replaced by `return False` — previously a bad
  `GetImageSize` would take down the whole hosting process.
- `GetMeasurementData_DynBitDepth` now returns `None` on failure instead of a zero-filled
  ndarray. **Breaking**: callers must check before use, or they will silently write an
  empty frame.
- Added `StartContinuousMeasurement_DynBitDepth()` and `StopContinuousMeasurement()`
  (wrapping `StartContinousMeasurement` / `StopContinousMeasurement` — note upstream's
  spelling differs between the C and Python names).
- Dropped an `UpdateStatus()` call from `ConnectToMultipleCameraServer`.

## C++ SDK — `lib/x64/greateyes.dll`

| | |
|---|---|
| Version | 22.8.2606.02-0-g96d4b4d |
| Source archive | `greateyes_sdk_c++_windows_22.8.2606.02-0-g96d4b4d.zip` |
| Archive sha256 | `0d2ccede34a33439f3842f0c0c6b386f0bc089c318621cae8828a4ac7a96e38b` |
| DLL sha256 | `9d37c0463bd882f07b44151c55609441fda7a529303914efc860c3840c545e14` |
| DLL md5 | `d1b8f2b2be5eb71d433a5788b64d355b` |
| DLL size | 431104 bytes |
| Retrieved | 2026-08-02 |

Only the **x64** build is vendored — the MAST Python is 64-bit, and `ctypes.WinDLL` would
reject the x86 build with `WinError 193`. The archive also ships `x86/`, `include/`
(`greateyes.h`, `greateyesBeta.h`) and import libraries; we need none of them at runtime.

This DLL imports only system libraries plus the MSVC runtime (`MSVCP140`, `VCRUNTIME140`,
`VCRUNTIME140_1`), so it can be loaded from anywhere. The previous 2021 DLL additionally
imported `geCommLib.dll` and could therefore only be loaded from the greateyesVision
install directory — that is why the DLL now lives here instead.

### Relationship to the greateyesVision install

`C:\Program Files\greateyes\greateyesVision\` is **not** modified by MAST. It still holds
the September 2021 `greateyes.dll` (md5 `17a245c60174f49e57a0df2ada69f559`) that we used
to load; the GUI application continues to use it. Leaving it alone keeps a working copy of
the old binary on every machine and avoids fighting the vendor's installer.

## Documentation

`docs/greateyes_doc-camera_sdk_c++_fw12_rev5.pdf` documents an older SDK revision and has
not been re-fetched. Treat the headers in the C++ archive as authoritative where they
disagree.

## Local modifications

None. `greateyesSDK.py` is upstream's file byte for byte, and is excluded from ruff in
`ruff.toml` so the formatter cannot introduce drift. Any local change belongs in a
**separate commit** on top of the commit that lands the pristine file, so that

```
git diff <pristine-commit> HEAD -- cameras/greateyes/sdk/greateyesSDK.py
```

always yields exactly our patch set — which is what makes the next SDK bump cheap.
