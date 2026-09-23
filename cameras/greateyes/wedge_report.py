"""
Record what a wedged greateyes camera looks like, while it is still wedged.

MAST_spec#104: a camera can sit with `DllIsBusy` true and no measurement that will ever
end, while `detected`, `connected` and `operational` all still read True. Two of these
have been seen, and they are not the same shape:

  A. **Exposing set, busy forever.** Band U, 2026-09-07: `StartMeasurement` never
     completed, so on_timer's `Exposing and not DllIsBusy` could never fire and the band
     sat at Acquiring|Exposing. From the outside this is indistinguishable from an
     exposure that is simply still running, which is why a trigger that only watches for
     "busy with nothing running" would miss it entirely.
  B. **Exposing down, busy anyway.** Band G, 2026-09-07: busy with nothing running,
     refusing every exposure, its only outward sign that both temperature getters return
     None. It recovered on its own -- same process, no power cycle -- somewhere inside a
     16-hour window, and NOTHING was logged in between, so even that duration is an upper
     bound on something nobody can recover from the record.

Nobody knows which layer either one is in, and that is the whole problem. A blocking
socket read inside the DLL and a leaked busy flag in the DLL's own state produce exactly
the same `DllIsBusy() == True`, and they call for opposite fixes: one wants a transport
timeout, the other wants a state reset that today only a process restart performs. The
question has been argued from logs that cannot settle it.

So this module records what does settle it -- see each function for what it discriminates
and why. Three observations are taken at the moment of the wedge (the busy flag across
devices, the sockets, the thread stacks), and a fourth runs continuously: every SDK call is
timed at its boundary, because "which call never returned" cannot be recovered from a
snapshot taken afterwards. It **observes only**: it opens no connection, kills nothing, and
makes no SDK call that can start work.

Deliberately NOT `stale_session.classify` or `.reachability`, though both are right here
and would answer "is the camera's server still up?". They open a fresh TCP connection to
the camera, and this runs against a camera in a state nobody understands. The one time a
probe was aimed at a camera in a delicate state -- `wait_until_accepting` against a
booting one -- that run was the slowest recorded and the poll was backed out of the probe
path. Not proof of disturbance at n=1, but the same caution applies to a camera that is
mid-wedge and might yet recover on its own, as G did. What we already hold a socket to,
we may look at; we do not go knocking.
"""

import contextlib
import ctypes
import sys
import threading
import time
import traceback
from collections import deque

import cameras.greateyes.sdk.greateyesSDK as ge  # noqa: N813
from cameras.greateyes import stale_session
from common.mast_logging import get_logger

logger = get_logger(__name__)

# The SDK addresses at most four devices ("max. 4 devices", greateyesSDK.py), so sweeping
# this range covers every band whatever the config assigns as its `device`.
SDK_MAX_DEVICES = 4

# Innermost frames kept per thread: enough to show which SDK call a thread is parked in,
# without reprinting the timer loop above it.
STACK_DEPTH = 8

# Threads whose stack passes through this file are the ones inside the SDK.
SDK_FILENAME = "greateyesSDK.py"

# Completed SDK calls kept for context. Enough to show the sequence that led into a wedge
# without holding the log hostage: at ~5 calls a second across four bands this is a few
# seconds of history, which is the window that matters.
CALL_HISTORY = 40

# ---------------------------------------------------------------------------------------
# SDK call tracing
#
# The stack walk above can say a thread is inside the SDK. It cannot say for how long, and
# it cannot say anything at all about a call that has already returned -- so "no thread is
# inside the SDK" leaves the obvious follow-up ("then which call was the last one, and did
# it return?") unanswerable. Worse, it is a racy snapshot: the first report of the 2026-09-22
# wedge caught a passing TemperatureControl_GetTemperature and made it look like a block,
# and only the second report ten minutes later disproved that.
#
# So every SDK call is timed at its boundary. `_in_flight` is what is inside the DLL right
# now, with the timestamp it went in; `_history` is what came back, with how long it took.
# Between them "which call never returned" stops being an inference.
#
# This works without touching the vendored wrapper -- which VENDOR.md requires stay
# byte-identical -- because all 49 of its DLL calls resolve the symbol INSIDE the function
# body (`geFunc = greateyesDLL.SomeName`) rather than once at import. Replacing that one
# module-level object with a proxy therefore intercepts every call there will ever be.
# ---------------------------------------------------------------------------------------

_trace_lock = threading.Lock()
_in_flight: dict[int, tuple[str, int | None, float, str]] = {}  # thread ident -> (fn, addr, t0, thread name)
_history: deque[tuple[str, int | None, float, float, str]] = deque(maxlen=CALL_HISTORY)
_installed = False


def _addr_of(args: tuple) -> int | None:
    """Best-effort device index for a call, or None.

    Every wrapper that takes one passes `addr` last, as a `c_int`. Read positionally rather
    than by name because the wrapper calls the DLL with bare positional ctypes objects, and
    guarded because a wrong guess here must cost a null in a log line, never an exception
    inside an SDK call.
    """
    with contextlib.suppress(Exception):  # see the docstring: a bad guess costs a null, not a raise
        if args and isinstance(args[-1], ctypes.c_int):
            return int(args[-1].value)
    return None


class _TracedFunc:
    """One DLL entry point, timed.

    Transparent on purpose: the wrapper assigns `restype` and `argtypes` on whatever it gets
    back from the DLL object, so those have to reach the real function or every call is
    marshalled wrongly.
    """

    __slots__ = ("_fn", "_name")

    def __init__(self, fn, name: str):
        object.__setattr__(self, "_fn", fn)
        object.__setattr__(self, "_name", name)

    def __setattr__(self, key, value):
        setattr(self._fn, key, value)  # restype / argtypes belong to the real function

    def __getattr__(self, key):
        return getattr(self._fn, key)

    def __call__(self, *args, **kwargs):
        ident, t0 = threading.get_ident(), time.monotonic()
        # Both sides under the lock. Each thread owns its own key, so the dict operations
        # never collide -- but `calls_in_flight` iterates this dict, and iterating one that
        # another thread is inserting into raises "changed size during iteration". The cost
        # is one uncontended acquire on a path that runs a few times a second.
        # tracing must never break the call it is timing
        with contextlib.suppress(Exception), _trace_lock:
            _in_flight[ident] = (self._name, _addr_of(args), t0, threading.current_thread().name)
        try:
            return self._fn(*args, **kwargs)
        finally:
            with contextlib.suppress(Exception), _trace_lock:  # as above
                name, addr, started, tname = _in_flight.pop(ident, (self._name, None, t0, ""))
                _history.append((name, addr, started, time.monotonic() - started, tname))


class _TracedDLL:
    """Stands in for the ctypes DLL object and hands out timed entry points."""

    __slots__ = ("_cache", "_dll")

    def __init__(self, dll):
        object.__setattr__(self, "_dll", dll)
        object.__setattr__(self, "_cache", {})

    def __getattr__(self, name):
        cache = self._cache
        if name not in cache:
            # Cached like ctypes caches its own function pointers, so the per-call lookup in
            # the wrapper stays a dict hit rather than rebuilding a wrapper 4 times a second.
            cache[name] = _TracedFunc(getattr(self._dll, name), name)
        return cache[name]


def install_call_tracer() -> None:
    """Start timing SDK calls. Idempotent; safe to call from every camera's constructor."""
    global _installed
    if _installed:
        return
    try:
        ge.greateyesDLL = _TracedDLL(ge.greateyesDLL)
        _installed = True
        logger.info("SDK call tracing installed")
    except Exception as e:  # noqa: BLE001 -- a diagnostic must not stop the service starting
        logger.error(f"could not install SDK call tracing: {type(e).__name__}: {e}")


def calls_in_flight() -> list[str]:
    """Which SDK calls are inside the DLL right now, and since when.

    This is the line the stack walk could not write. A call sitting here for minutes is a
    call that never returned -- stated from its own entry timestamp, not inferred from one
    snapshot of a stack.
    """
    with _trace_lock:
        snapshot = list(_in_flight.values())
    if not snapshot:
        return ["    nothing is inside the DLL right now"]
    now = time.monotonic()
    return [
        f"    {name}(addr={addr}) on {tname} -- in the DLL for {now - t0:.1f}s"
        for name, addr, t0, tname in sorted(snapshot, key=lambda s: s[2])
    ]


def recent_calls() -> list[str]:
    """The last completed SDK calls, newest last, with how long each took."""
    with _trace_lock:
        history = list(_history)
    if not history:
        return ["    no completed calls recorded"]
    return [f"    {name}(addr={addr}) on {tname} took {took:.3f}s" for name, addr, _, took, tname in history]


def dll_busy_by_device(current: int) -> list[str]:
    """`DllIsBusy` for every SDK device index, not just the band that noticed.

    Settles whether the DLL's busy state is per-device or shared. `SetBusyTimeout`'s own
    documentation says a call "will try to get a slot", which is the language of a lock --
    and if that lock is global, one wedged band makes the other three report busy and the
    four bands were never independent to begin with. One wedged band beside three idle
    neighbours says the opposite. Nothing in the record answers this today, and it decides
    whether a per-camera recovery is even coherent.

    Swept only from the wedge path, never per tick. Every SDK call contends for whatever
    that slot is, and MAST_spec#88 is the standing lesson on what parking camera threads
    on a lock costs. Four extra calls per wedge report is affordable; four per tick, on
    four bands at 1 Hz, is the mistake that lesson was about.
    """
    lines = []
    for addr in range(SDK_MAX_DEVICES):
        try:
            busy = ge.DllIsBusy(addr=addr)
        except Exception as e:  # noqa: BLE001 -- a diagnostic must not raise into the timer
            lines.append(f"    device {addr}: DllIsBusy raised {type(e).__name__}: {e}")
            continue
        mine = "  <- this band" if addr == current else ""
        lines.append(f"    device {addr}: busy={busy}{mine}")
    return lines


def thread_stacks() -> list[str]:
    """Where every thread is, with the ones inside the SDK shown in full.

    This is the discriminator the other two observations cannot supply, and the one thing
    never yet recorded for a wedge. A band thread parked inside
    `ge.StartMeasurement_DynBitDepth` means the SDK call never returned: the wedge is in
    the transport, below Python, and a timeout is what would end it. The DLL reporting
    busy while NO thread is anywhere inside the SDK means the call returned and the busy
    state outlived it: there is nothing to unblock, and only a state reset ends that.

    Both produce `DllIsBusy() == True`. They are told apart here and nowhere else.

    `sys._current_frames()` is private, and is the only way to see another thread's stack
    from inside the process. It gives a snapshot per thread, so a frame may be stale by
    the time it is formatted -- which does not matter for a thread that has been parked in
    the same call for minutes, the only case this runs in.

    KNOWN BLIND SPOT, measured 2026-09-22: this sees only threads Python created. The
    service had **87 OS threads and 25 Python ones**, so two thirds of the process is
    invisible here -- and the DLL's own workers are in that two thirds (its exports include
    a geDllRequestMsg / geDllReplyMsg message pump). "No thread is inside the SDK" therefore
    means "no PYTHON thread is", which is not the same claim. Seeing the rest needs a native
    tool: `py-spy dump --pid <pid> --native` reaches into greateyes.dll and names its frames
    (it exports 266 symbols, so nearest-symbol resolution is informative without PDBs), and
    Process Explorer or WinDbg reach the non-Python threads py-spy also omits.
    """
    try:
        # Private, and the only way to see another thread's stack from inside the process.
        frames = sys._current_frames()
    except Exception as e:  # noqa: BLE001 -- a diagnostic must not raise into the timer
        return [f"    could not read thread stacks: {type(e).__name__}: {e}"]

    names = {t.ident: t.name for t in threading.enumerate()}
    in_sdk: list[str] = []
    elsewhere: list[str] = []

    for ident, frame in frames.items():
        name = names.get(ident, f"thread-{ident}")
        try:
            stack = traceback.extract_stack(frame)
        except Exception as e:  # noqa: BLE001
            elsewhere.append(f"    {name}: stack unavailable ({type(e).__name__}: {e})")
            continue
        if not stack:
            continue
        if any(frm.filename.endswith(SDK_FILENAME) for frm in stack):
            in_sdk.append(f"    {name}: INSIDE THE SDK")
            for frm in stack[-STACK_DEPTH:]:
                in_sdk.append(f"        {frm.filename}:{frm.lineno} in {frm.name}")
        else:
            innermost = stack[-1]
            elsewhere.append(f"    {name}: {innermost.name} ({innermost.filename}:{innermost.lineno})")

    if not in_sdk:
        # Stated rather than left as an absence: "no thread is in the SDK" is a positive
        # finding here, and the whole point of collecting this.
        in_sdk = ["    no thread is inside the SDK -- the busy state has outlived its call"]
    return in_sdk + elsewhere


def sockets_to_camera(ipaddr: str) -> list[str]:
    """The sockets this machine already holds to the camera.

    Read-only: `sessions_to` parses the connection table and opens nothing. What it
    separates is a connection that is still ESTABLISHED underneath a wedged DLL -- the
    half-open case, where the peer is gone and the local end has not noticed -- from one
    that is already gone, which would mean the DLL is busy with no transport left to be
    blocked on.

    Returns None-handling inline: `sessions_to` distinguishes "nothing connected" from "I
    could not read the table", and collapsing those would let a failed check read as
    evidence.
    """
    sessions = stale_session.sessions_to(ipaddr)
    if sessions is None:
        return ["    could not read the connection table -- no conclusion available"]
    if not sessions:
        return [f"    no socket on this machine to {ipaddr} -- nothing left to be blocked on"]
    return [f"    {session}" for session in sessions]


def describe(band: str, ipaddr: str, device: int, busy_for: float, exposing: bool) -> list[str]:
    """One wedge report, as lines for the caller to log. Logs nothing itself."""
    shape = (
        "A (Exposing set: a measurement that never completed)"
        if exposing
        else "B (Exposing down: busy with nothing running)"
    )
    lines = [
        f"WEDGE REPORT for band {band}: DllIsBusy has been true for {busy_for:.0f}s -- shape {shape}",
        "  DllIsBusy across devices (is the DLL's busy slot per-device or shared?):",
    ]
    lines += dll_busy_by_device(device)
    lines.append(f"  sockets to the camera at {ipaddr}:")
    lines += sockets_to_camera(ipaddr)
    lines.append("  SDK calls in flight (timed at the boundary, not inferred from a stack):")
    lines += calls_in_flight()
    lines.append("  last completed SDK calls, oldest first:")
    lines += recent_calls()
    lines.append("  threads (is anything still inside the SDK?):")
    lines += thread_stacks()
    return lines
