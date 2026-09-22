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

So this module records, at the moment of the wedge, the three things that do settle it --
see each function for what it discriminates and why. It **observes only**: it opens no
connection, kills nothing, and makes no SDK call that can start work.

Deliberately NOT `stale_session.classify` or `.reachability`, though both are right here
and would answer "is the camera's server still up?". They open a fresh TCP connection to
the camera, and this runs against a camera in a state nobody understands. The one time a
probe was aimed at a camera in a delicate state -- `wait_until_accepting` against a
booting one -- that run was the slowest recorded and the poll was backed out of the probe
path. Not proof of disturbance at n=1, but the same caution applies to a camera that is
mid-wedge and might yet recover on its own, as G did. What we already hold a socket to,
we may look at; we do not go knocking.
"""

import sys
import threading
import traceback

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
    lines.append("  threads (is anything still inside the SDK?):")
    lines += thread_stacks()
    return lines
