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
import datetime
import os
import re
import socket
import sys
import threading
import time
import traceback
from collections import deque

import cameras.greateyes.sdk.greateyesSDK as ge  # noqa: N813
from cameras.greateyes import stale_session
from common.mast_logging import get_logger, observing_night

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

# Every single call, in order, with its arguments -- the artefact for greateyes support, whose
# first question about any fault is which sequence produced it. Deliberately LOCAL and not the
# operational share: this is written on the SDK call path (see _write_trace for why it has to
# be), and the share is the one thing on this machine that can block for seconds.
#
# Its own directory so that pruning is safe by construction: everything matching the name
# pattern in here was written by this module, and nothing else has to be reasoned about.
#
# NOT under Filer's ram root, and that is load-bearing. `_relocate_products` walks that root
# and carries off every SUBFOLDER directly holding non-bookkeeping files -- which is exactly
# what this folder is. Here the ram root is D:/MAST/ (Filer picks D: when it is present, C:
# only otherwise), so C:\MAST is untouched by the sweep. On a machine with no D: drive the ram
# root would BE C:/MAST/ and the sweeper would relocate this folder to the share on the next
# startup, taking the trace off the machine and out from under the open handle.
#
# Set TRACE_DIR to None to turn the per-call file off; the in-memory trace is independent.
TRACE_DIR = r"C:\MAST\Greateyes-SDK-Trace"

# One file per observing night, and the last this many kept.
#
# The night rather than the calendar day, taken from the one place that anchor is written down
# (common.mast_logging.observing_night, MAST_common#28). A night turns at 12:00 UTC and spans
# local midnight, so dating by the calendar would split every night's trace across two files
# -- and would put the trace on a different clock from the service log and the product folders,
# which is the one thing a support artefact must not do.
TRACE_NIGHTS_KEPT = 7

# Filenames this module owns. Pruning deletes only what matches, so a stray file in the
# directory is left alone rather than swept up with the traces.
TRACE_NAME_RE = re.compile(r"^greateyes-sdk-trace-\d{4}-\d{2}-\d{2}\.txt$")

# How often the writer re-derives the current night. Rolling over up to this late costs a few
# lines in the previous night's file and keeps a datetime call off most of the write path.
NIGHT_RECHECK_SECONDS = 60

# Longest rendering of a single argument or return value, so one oversized buffer cannot turn
# a line into a page.
TRACE_VALUE_CHARS = 120

# Pointer targets the trace will dereference to show what the DLL wrote back. Deliberately a
# WHITELIST of scalars, not "anything with .contents": the wrapper also passes pointers to
# image buffers, and following one of those would put megabytes in a log line -- while anything
# unanticipated must not be dereferenced at all. Everything outside this renders as its type.
#
# Worth the care because of what the commonest one carries: 31 of the wrapper's calls pass
# `pointer(c_Status)`, the statusMSG the SDK writes its status code into. 19 of the 50 wrappers
# never call UpdateStatus(), so for those the status is written by the DLL and then dropped on
# the floor -- which is the whole of MAST_spec#94. Reading it here recovers the true status of
# EVERY call, including the ones the Python layer throws away.
TRACE_DEREF_TYPES = (
    ctypes.c_bool,
    ctypes.c_byte,
    ctypes.c_double,
    ctypes.c_float,
    ctypes.c_int,
    ctypes.c_long,
    ctypes.c_short,
    ctypes.c_ubyte,
    ctypes.c_uint,
    ctypes.c_ulong,
    ctypes.c_ushort,
)

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

# The per-call file. Its own lock, not _trace_lock: the in-memory bookkeeping must not be held
# across a write. `_trace_file_broken` latches on the first failure -- if the file cannot be
# written, every subsequent SDK call must not pay for discovering that again.
_file_lock = threading.Lock()
_trace_file = None
_trace_file_broken = False
_call_seq = 0
# The observing night the open handle belongs to, and when that was last derived. Compared
# rather than recomputed per call; see _write_trace.
_trace_night: str | None = None
_night_checked_at: float | None = None
# Captured before the DLL is wrapped, for every night's header. Read afterwards it would be a
# traced call, and asking for it from inside a header write would recurse into the writer.
_dll_version: str | None = None

# Installation is racy without this, and the race is not cosmetic. greateyes.py builds the four
# cameras on four concurrent threads (`make-deepspec-camera-<band>`), so GreatEyes.__init__ --
# and this with it -- runs four times at once. An unguarded check-then-set let three of them
# through on 2026-09-23, which showed up as three copies of the trace header.
#
# The headers were the visible symptom; the danger was the other outcome of the same race. All
# three happened to read the original WinDLL before any assigned, so the last write won and the
# DLL ended up wrapped exactly once. Had one thread assigned before another read, the second
# would have wrapped the first's proxy -- and every SDK call would then be traced once per
# layer, quietly doubling the artefact the whole exercise exists to produce.
_install_lock = threading.Lock()


def _render(value) -> str:
    """One argument or return value, as something a reader can match against the SDK manual.

    Best-effort by design: these are raw ctypes objects, and an argument nobody anticipated
    must degrade to its type name rather than raise inside a call this is only observing.
    """
    with contextlib.suppress(Exception):  # see the docstring: degrade to the type name, never raise
        if isinstance(value, ctypes.c_char_p):
            raw = value.value
            return repr(raw.decode("ascii", "replace")) if raw is not None else "None"
        if hasattr(value, "value") and isinstance(value.value, (int, float, bool, bytes)):
            return repr(value.value)[:TRACE_VALUE_CHARS]
        if isinstance(value, (int, float, bool, str, bytes)) or value is None:
            return repr(value)[:TRACE_VALUE_CHARS]
    return type(value).__name__


def _render_outputs(args: tuple) -> str:
    """What the DLL wrote into the caller's pointers, read after the call returned.

    The half of a call the entry line cannot show. On the way in these are uninitialised
    memory; on the way out they carry the status code, the model id, the image geometry --
    the answers support asks about, and in the statusMSG case an answer the Python wrapper
    frequently discards (MAST_spec#94).

    Safe to do here, and only here. The pointers are created by the wrapper itself and are
    still referenced by `args` while this runs, so none of them can have been freed; only the
    whitelisted scalar targets are followed; and a NULL pointer raises rather than reading
    address zero, which the suppression turns into a skipped field instead of a dead service.
    """
    out = []
    for i, value in enumerate(args):
        contents = None
        with contextlib.suppress(Exception):
            target = getattr(type(value), "_type_", None)
            if not hasattr(value, "contents") or target is None:
                continue
            if target in TRACE_DEREF_TYPES:
                contents = repr(value.contents.value)[:TRACE_VALUE_CHARS]
            elif target is ctypes.c_char_p:
                raw = value.contents.value
                contents = repr(raw.decode("ascii", "replace")) if raw is not None else "None"
            else:
                # An array or a struct: named, never followed. This is where the image
                # buffers are, and a trace line is not the place for four megapixels.
                contents = f"<{getattr(target, '__name__', target)}>"
        if contents is not None:
            out.append(f"#{i}={contents}")
    return f" out({', '.join(out)})" if out else ""


def _prune_old_traces() -> None:
    """Keep the newest TRACE_NIGHTS_KEPT nights and delete the rest.

    Only names matching TRACE_NAME_RE, and only files -- a stray anything else in the directory
    is left where it is rather than swept up with the traces. ISO dates sort chronologically,
    so lexicographic order is the age order.
    """
    with contextlib.suppress(Exception):
        # Filtered to FILES before the slice, not inside the loop. A directory that happens to
        # match the pattern would otherwise occupy a deletion slot and survive it, so one
        # too few real traces would be removed.
        names = sorted(
            n for n in os.listdir(TRACE_DIR) if TRACE_NAME_RE.match(n) and os.path.isfile(os.path.join(TRACE_DIR, n))
        )
        for name in names[:-TRACE_NIGHTS_KEPT] if len(names) > TRACE_NIGHTS_KEPT else []:
            os.remove(os.path.join(TRACE_DIR, name))


def _open_for_night(night: str) -> None:
    """Open tonight's trace file, header it, and prune what has aged out.

    Called with _file_lock held. Every file gets its own header: a night's trace is handed to
    the vendor on its own, so each has to say which machine and which DLL produced it rather
    than referring back to a file from six nights ago.
    """
    global _trace_file, _trace_night
    if _trace_file is not None:
        with contextlib.suppress(Exception):
            _trace_file.close()
        _trace_file = None
    os.makedirs(TRACE_DIR, exist_ok=True)
    path = os.path.join(TRACE_DIR, f"greateyes-sdk-trace-{night}.txt")
    _trace_file = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    _trace_night = night
    _trace_file.write(
        f"# greateyes SDK call trace -- {socket.gethostname()} -- observing night {night}\n"
        f"# opened {_stamp()}, DLL version {_dll_version!r}\n"
        f"# '>' call entered, '<' returned. #NNNNNNN pairs them across threads.\n"
        f"# An entry with no matching return is a call that never came back.\n"
    )
    _prune_old_traces()


def _write_trace(line: str) -> None:
    """Append one line to the per-call trace file, opening it on first use.

    ON THE CALL PATH, which reverses the rule the rest of this module follows. The reasoning:
    a queue drained by the timer would lose whatever had not been drained when the process was
    killed -- and the calls immediately before a hang or a crash are precisely the ones support
    will ask about. A trace that is complete except at the interesting moment is not worth
    keeping. The file is local and line-buffered, so each line reaches the OS on its own and
    survives the process dying; only a machine crash could lose it.

    A failure latches rather than retrying: if the directory has gone, every later SDK call must
    not pay to rediscover that.
    """
    global _trace_file_broken, _night_checked_at
    if TRACE_DIR is None or _trace_file_broken:
        return
    try:
        with _file_lock:
            # Deriving the night costs a datetime per call, so it is done on first use and then
            # at most every NIGHT_RECHECK_SECONDS. Between checks the open handle is used as-is.
            now = time.monotonic()
            if _trace_file is None or _night_checked_at is None or now - _night_checked_at >= NIGHT_RECHECK_SECONDS:
                _night_checked_at = now
                night = observing_night(datetime.datetime.now(datetime.UTC))
                if night != _trace_night:
                    _open_for_night(night)
            _trace_file.write(line + "\n")
    except Exception as e:  # noqa: BLE001 -- never raise into an SDK call
        _trace_file_broken = True
        logger.error(f"per-call trace disabled, could not write under {TRACE_DIR}: {type(e).__name__}: {e}")


def _stamp() -> str:
    """UTC with milliseconds, spelled the way the service log spells it, so the two line up."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


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
        global _call_seq
        ident, t0 = threading.get_ident(), time.monotonic()

        # An ENTRY line as well as an exit one, which doubles the file and is the whole point:
        # a call that never returns writes no exit line, so with exits alone the one call that
        # matters would be the one call absent from the trace. An unmatched `>` at the end of
        # the file IS the finding.
        #
        # The id is what makes that legible with four bands interleaving -- `>` and `<` for one
        # call can be many lines apart, and matching them by name would be guesswork.
        seq = 0
        with contextlib.suppress(Exception):
            with _file_lock:
                _call_seq += 1
                seq = _call_seq
            tname = threading.current_thread().name
            _write_trace(f"{_stamp()} #{seq:07d} > {self._name}({', '.join(_render(a) for a in args)}) [{tname}]")

        # Both sides under the lock. Each thread owns its own key, so the dict operations
        # never collide -- but `calls_in_flight` iterates this dict, and iterating one that
        # another thread is inserting into raises "changed size during iteration". The cost
        # is one uncontended acquire on a path that runs a few times a second.
        # tracing must never break the call it is timing
        with contextlib.suppress(Exception), _trace_lock:
            _in_flight[ident] = (self._name, _addr_of(args), t0, threading.current_thread().name)
        result, raised = None, None
        try:
            result = self._fn(*args, **kwargs)
            return result
        except BaseException as e:
            raised = e
            raise
        finally:
            with contextlib.suppress(Exception):
                outcome = f"!! {type(raised).__name__}: {raised}" if raised is not None else f"-> {_render(result)}"
                # Output parameters only on the way out. On the way in they are uninitialised,
                # and printing them there would put noise where the sequence should be.
                _write_trace(
                    f"{_stamp()} #{seq:07d} < {self._name} {outcome}{_render_outputs(args)} "
                    f"({time.monotonic() - t0:.3f}s) [{threading.current_thread().name}]"
                )
            with contextlib.suppress(Exception), _trace_lock:  # as above
                name, addr, started, tname = _in_flight.pop(ident, (self._name, None, t0, ""))
                took = time.monotonic() - started
                _history.append((name, addr, started, took, tname))


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
    global _installed, _dll_version
    # Check and set under the lock, not around it: four camera-construction threads reach this
    # simultaneously, and a bare flag lets more than one through. See _install_lock.
    with _install_lock:
        if _installed:
            return
        try:
            # Read BEFORE wrapping, deliberately: afterwards this is itself a traced call, and
            # asking for it from inside the file's first write would recurse into the writer.
            _dll_version = ge.GetDLLVersion()
            ge.greateyesDLL = _TracedDLL(ge.greateyesDLL)
            _installed = True
            # No header written here any more: every night's file needs one of its own, so
            # _open_for_night writes it as part of opening. The first traced call triggers that.
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
