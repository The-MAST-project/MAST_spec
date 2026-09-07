"""
Detect what, on this machine, is holding a session to a greateyes camera.

A failed `ConnectToSingleCameraServer` has two quite different explanations, and
they call for opposite responses:

  A. A live process here still holds the session. That is MAST_spec#77 --
     importing this repo starts camera threads at module scope, so a stray
     interpreter keeps an ESTABLISHED socket open per band. Killing it frees the
     camera at once, and the 25 s power-cycle-and-boot is time thrown away.
  B. Nothing here holds it. The previous process was terminated rather than
     stopped (zero `ShuttingDown` lines across six restarts), Windows closed its
     sockets, and the camera is left in a state only a power cycle clears. There
     is nothing local to kill.

What B turned out to be, measured on mast-ns-spec 2026-09-07 across two restarts,
all four bands: `holder=nobody server=unreachable`, with the outlet already ON.
The camera was powered, had been serving nine minutes earlier, and answered
nothing at all on its server port. So the camera is not holding a stale session
against a dead peer -- it stops listening entirely once its client dies, and only
the power cycle restarts it. That rules out retrying before the cycle, and it
means disconnecting on shutdown would not have prevented this.

The fixed wait after the cycle looked like what was left to win, and it is not:
measured the same day, `boot_delay` is too SHORT -- a camera was still dead to TCP
28.5 s after its power cycle. `wait_until_accepting` below was written for that and
is currently called by nothing; see the commented block in greateyes.py's probe for
why it was backed out and what a second attempt should do differently.

This module answers the observable half -- who on this machine has a socket open
to that camera -- and **kills nothing**. Which of A or B applies is the caller's
conclusion, and it is only meaningful once a connect has actually failed:
"no local session" is the ordinary state of a healthy machine.

Knowing nothing local holds the camera is not yet enough to act on, because it
does not separate "the camera is holding a session against a dead peer" from
"the camera's server is not listening at all". Those look identical from here and
point at opposite fixes -- session hygiene versus not blind-sleeping through the
boot. So when nothing local is holding the camera we additionally ask the camera
itself, with a plain TCP connect to the server port.

Note the server is at the camera, not here: the SDK's own comment for
`ConnectToSingleCameraServer` describes MultiServerMode, "up to four
greateyesCameraServers, each operating one camera". So there is never a local
server process to find -- only a local client holding a socket.

Deliberately does not cache across the four bands. They probe within ~200 ms of
each other, so a shared result would want a lock, and MAST_spec#88 is the
standing lesson about parking camera threads on a lock during connect. Four
cheap subprocesses on a failed probe is the better trade.
"""

import csv
import io
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum

from common.mast_logging import get_logger

logger = get_logger(__name__)

# Neither command is slow, but both run on the band's timer thread, so neither
# gets to hang a probe.
COMMAND_TIMEOUT_SECONDS = 5

# The greateyes camera server's TCP port. The DLL owns the connection, so this is
# nowhere in the Python layer; it was read off a live connection table on
# mast-ns-spec (2026-09-07), where all four bands showed 192.168.1.23x:12345.
# A module constant rather than a config field on purpose: GreateyesProbingConfig
# lives in common/, which is one shared clone across four projects, and this is
# still experimental.
CAMERA_SERVER_PORT = 12345

# Short, because it runs on the band's timer thread and a camera that is not
# serving costs the whole timeout -- always, on this host, which drops rather than
# refuses (see ServerReachability.REFUSED). Only the failure path pays it, only
# when nothing local holds the camera, and the four bands pay it concurrently, so
# it is ~2 s against the ~46 s the failure path already costs.
CONNECT_TIMEOUT_SECONDS = 2

# How often wait_until_accepting() retries. Only paces the loop when a probe returns
# faster than this; an unanswered one already takes CONNECT_TIMEOUT_SECONDS.
POLL_INTERVAL_SECONDS = 2

# States in which a socket is still associated with the camera. CLOSE_WAIT and the
# FIN_WAIT_* pair matter as much as ESTABLISHED: the camera end can still hold the
# session while this end is part way through tearing down. SYN_SENT is here because
# an in-flight connect is also an explanation for what we are seeing.
LIVE_TCP_STATES = frozenset(
    {
        "ESTABLISHED",
        "SYN_SENT",
        "FIN_WAIT_1",
        "FIN_WAIT_2",
        "CLOSE_WAIT",
        "CLOSING",
        "LAST_ACK",
    }
)


@dataclass(frozen=True)
class Session:
    """One TCP socket on this machine whose foreign address is a camera."""

    local_port: int
    remote_addr: str
    remote_port: int
    state: str
    pid: int
    image: str | None  # process image name; None when it could not be read

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_TCP_STATES

    def __str__(self) -> str:
        who = f"pid={self.pid}" + (f" ({self.image})" if self.image else "")
        return f"local:{self.local_port} -> {self.remote_addr}:{self.remote_port} {self.state} {who}"


class SessionHolder(StrEnum):
    """Who, on this machine, holds a live socket to the camera."""

    OTHER_PROCESS = "other-process"  # another live process here -- MAST_spec#77's case
    THIS_PROCESS = "this-process"  # our own socket; we are already connected
    NOBODY = "nobody"  # no local socket at all
    UNKNOWN = "unknown"  # the check itself could not run


class ServerReachability(StrEnum):
    """What the camera's own server does with a plain TCP connect."""

    ACCEPTING = "accepting"  # completed a handshake: the server is up and taking connections
    REFUSED = "refused"  # answered with RST: the camera is up, the server is not listening
    UNREACHABLE = "unreachable"  # no answer at all: powered off, still booting, or dropped
    NOT_CHECKED = "not-checked"  # a local process holds the camera, so the answer would not change anything
    UNKNOWN = "unknown"  # the check itself could not run

    # REFUSED is not expected to appear on mast-ns-spec: measured 2026-09-07, this host
    # drops SYNs to closed ports rather than answering RST -- even 127.0.0.1 times out
    # instead of refusing. It is kept because the distinction is real where a stack does
    # answer, and because both it and UNREACHABLE lead to the same conclusion here. The
    # split that carries the weight is ACCEPTING versus not.


def _image_names(pids: set[int]) -> dict[int, str]:
    """Map pid -> image name via tasklist. Best effort: an empty map is not an error."""
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"could not run tasklist, sessions will be reported without an image name: {e}")
        return {}

    names: dict[int, str] = {}
    for fields in csv.reader(io.StringIO(completed.stdout)):
        # "python.exe","14188","Console","1","123,456 K"
        if len(fields) < 2:
            continue
        try:
            pid = int(fields[1])
        except ValueError:
            continue
        if pid in pids:
            names[pid] = fields[0]
    return names


def sessions_to(ipaddr: str) -> list[Session] | None:
    """
    Every TCP socket on this machine whose foreign address is `ipaddr`.

    Returns None when the connection table could not be read at all. That is
    deliberately distinct from an empty list: "nothing is connected" and "I could
    not tell" support different conclusions, and collapsing them would let a
    failed check masquerade as evidence.
    """
    if sys.platform != "win32":
        logger.debug(f"the stale-session check is implemented for Windows only, not '{sys.platform}'")
        return None

    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # A diagnostic must never break the probe it is there to explain.
        logger.error(f"could not run netstat: {e}")
        return None

    sessions: list[Session] = []
    for line in completed.stdout.splitlines():
        fields = line.split()
        # A TCP row is: proto, local, foreign, state, pid. Headers, blank lines and
        # (should `-p TCP` ever not be honoured) shorter UDP rows are skipped by
        # matching on shape rather than trusting the filter.
        if len(fields) != 5 or fields[0] != "TCP":
            continue
        local, foreign, state, pid = fields[1:5]
        foreign_addr, _, foreign_port = foreign.rpartition(":")
        if foreign_addr != ipaddr:
            continue
        try:
            sessions.append(
                Session(
                    local_port=int(local.rpartition(":")[2]),
                    remote_addr=foreign_addr,
                    remote_port=int(foreign_port),
                    state=state,
                    pid=int(pid),
                    image=None,
                )
            )
        except ValueError:
            continue

    if not sessions:
        return sessions

    names = _image_names({s.pid for s in sessions})
    return [Session(**{**vars(s), "image": names.get(s.pid)}) for s in sessions]


def classify(ipaddr: str) -> tuple[SessionHolder, list[Session]]:
    """
    Who holds a live socket to `ipaddr`, and the sockets that say so.

    OTHER_PROCESS wins over THIS_PROCESS when both are present: it is the only one
    of the two that is actionable.
    """
    sessions = sessions_to(ipaddr)
    if sessions is None:
        return SessionHolder.UNKNOWN, []

    live = [s for s in sessions if s.is_live]
    if not live:
        return SessionHolder.NOBODY, sessions

    ours = os.getpid()
    if any(s.pid != ours for s in live):
        return SessionHolder.OTHER_PROCESS, sessions
    return SessionHolder.THIS_PROCESS, sessions


def reachability(
    ipaddr: str, port: int = CAMERA_SERVER_PORT, timeout: float = CONNECT_TIMEOUT_SECONDS
) -> ServerReachability:
    """
    Ask the camera's own server what it does with a plain TCP connect.

    Only ever called after the SDK's connect has already failed, so there is no
    working session to disturb: the worst case is one handshake the camera would
    have accepted anyway, closed immediately. It is deliberately not called on a
    healthy probe.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((ipaddr, port))
    except ConnectionRefusedError:
        # An RST is an answer: the host is up, nothing is listening on the port.
        return ServerReachability.REFUSED
    except (TimeoutError, OSError):
        # No answer, or the network said the host cannot be reached. Both mean the
        # camera is not in a state to talk, which is the same conclusion here.
        return ServerReachability.UNREACHABLE
    except Exception as e:  # noqa: BLE001 -- a diagnostic must never break the probe it explains
        logger.error(f"unexpected error probing {ipaddr}:{port}: {e}")
        return ServerReachability.UNKNOWN
    finally:
        sock.close()

    return ServerReachability.ACCEPTING


def wait_until_accepting(ipaddr: str, budget_seconds: float, port: int = CAMERA_SERVER_PORT) -> float | None:
    """
    Wait for the camera's server to start answering, and return how long that took.

    Returns the elapsed seconds as soon as a connect succeeds, or None if the
    budget ran out first.

    NOT CURRENTLY CALLED. Kept, tested, and left here deliberately -- the probe path
    reverted to sleeping boot_delay on 2026-09-07 because polling opens ~12 TCP
    connections to a booting camera and the one run that did so was the slowest
    recorded. Read greateyes.py's probe before re-enabling this.

    This replaces sleeping through `boot_delay` blind. It can only be faster: the
    budget is the same, the probes happen inside it, and the last probe's timeout
    is trimmed so the total never overruns. Its second job is to measure -- nobody
    knows how long a greateyes camera actually takes to start serving after a power
    cycle, and the returned figure is that number, per band, in the log.
    """
    started = time.monotonic()
    deadline = started + budget_seconds

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None

        attempted_at = time.monotonic()
        # Trimmed to `remaining` so an unanswered probe cannot push us past the budget.
        if reachability(ipaddr, port, timeout=min(CONNECT_TIMEOUT_SECONDS, remaining)) is ServerReachability.ACCEPTING:
            return time.monotonic() - started

        # An unanswered SYN already costs the timeout on this host, so this usually
        # sleeps for nothing. It matters where the camera answers fast and negatively
        # (an RST), which would otherwise spin.
        idle = POLL_INTERVAL_SECONDS - (time.monotonic() - attempted_at)
        time.sleep(max(0.0, min(idle, deadline - time.monotonic())))


def describe(ipaddr: str, after_power_cycle: bool = False) -> list[str]:
    """
    Lines to log next to a failed connect, saying which failure mode this is.

    `after_power_cycle` says which probe this is, and it changes what an accepting
    server means: on a process's first probe it is a camera holding a session
    against a peer that is gone, but after a power cycle it is just a camera that
    has finished booting. Without it this said "the session is stranded" at
    08:57:19 on 2026-09-07, when the camera had done nothing of the sort.

    ASCII only, per common/CLAUDE.md: the daily file and the console do not agree
    about what they can render, and the file is what gets read the morning after.
    """
    holder, sessions = classify(ipaddr)

    # Only worth asking the camera when nothing here explains the failure. If a
    # local process holds it, that is already the actionable answer, and the probe
    # would cost CONNECT_TIMEOUT_SECONDS to tell us nothing new.
    reach = reachability(ipaddr) if holder is SessionHolder.NOBODY else ServerReachability.NOT_CHECKED

    lines = [f"stale-session check for {ipaddr}: holder={holder.value} server={reach.value}"]
    lines += [f"  {s}" for s in sessions]

    if holder is SessionHolder.OTHER_PROCESS:
        lines.append(
            "  -> a live process on this machine holds the camera (MAST_spec#77). Killing it would free the "
            "camera immediately; the power cycle and boot_delay that follow are wasted time."
        )
    elif holder is SessionHolder.THIS_PROCESS:
        lines.append(
            "  -> this process already holds a socket to the camera. Not a leftover: look at our own connect "
            "sequence rather than at other processes."
        )
    elif holder is SessionHolder.UNKNOWN:
        lines.append("  -> could not read the connection table, so this says nothing either way.")
    elif reach is ServerReachability.ACCEPTING and after_power_cycle:
        lines.append(
            "  -> the camera has finished booting and is accepting connections; the SDK connect simply ran "
            "before it was ready. Nothing is stranded. Measured 2026-09-07: a camera can still be dead to TCP "
            "28.5 s after a power cycle, which is longer than boot_delay."
        )
    elif reach is ServerReachability.ACCEPTING:
        lines.append(
            "  -> nothing here holds the camera, yet its server accepts connections. The session is stranded "
            "at the camera end against a peer that is gone. Disconnecting on shutdown is the fix; the power "
            "cycle is only what clears it after the fact."
        )
    elif reach is ServerReachability.REFUSED:
        lines.append(
            "  -> the camera is up but its server is not listening, so there is no session to clear. The power "
            "cycle is justified; what is worth fixing is the blind boot_delay that follows it."
        )
    elif reach is ServerReachability.UNREACHABLE:
        lines.append(
            "  -> the camera did not answer at all: powered off, still booting, or unreachable. The power cycle "
            "is justified, and what follows it has to outlast the boot -- measured 2026-09-07, a camera was "
            "still dead to TCP 28.5 s after its cycle, which is why boot_delay went from 25 s to 60 s."
        )
    else:
        lines.append("  -> nothing here holds the camera, and the server probe itself could not run.")

    return lines
