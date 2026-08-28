# Bench Buddy - desktop control console for the Keysight 34461A multimeter.
# Copyright (C) 2026 zombu4
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <https://www.gnu.org/licenses/>.

"""SCPI transport for the Keysight 34461A.

One :class:`ScpiLink` owns exactly one session to the instrument and serialises
every exchange behind a re-entrant lock, so a link may be shared by several
threads.  The *session* is provided by a swappable transport; the discipline
around it is identical whichever transport is in use.

Why there are two transports
----------------------------

A raw socket on port 5025 has no session semantics whatsoever.  The instrument
cannot tell a dead client from a quiet one, so anything the client started
keeps running after the client dies.  Measured on this unit on 2026-08-27, with
``TRIG:SOUR IMM; TRIG:COUN INF; SAMP:COUN 1; INIT`` running and the client
process ended with ``TerminateProcess`` so that no handler of ours could
possibly run:

===================================  ============================  ============
Transport                            After a hard client kill      Panel left
===================================  ============================  ============
Raw socket, port 5025                still acquiring at t+60 s     frozen
HiSLIP (``hislip0``, port 4880)      still acquiring at t+60 s     frozen
VXI-11 (``inst0``, RPC over 111)     **aborted and cleared, 2 s**  free-running
===================================  ============================  ============

HiSLIP does not help: this firmware leaves the acquisition running even after a
*clean* HiSLIP session close, so it is not implemented here.  VXI-11 does help,
because the instrument's own RPC server performs a device clear when the last
VXI-11 link is destroyed — and destroying the link is something the operating
system does for us when the process dies, whether or not the process was given
any chance to tidy up.  That is a deadman the *instrument* enforces, which is
the only kind that survives a crash.

Two measured caveats are worth knowing:

* The clear fires when the **last** VXI-11 link goes, not when *a* link goes.
  Verified by holding a second, idle VXI-11 link open across the kill: the
  acquisition then kept running.  This application holds exactly one link
  (IO-DISCIPLINE.md rule 3), so the protection applies — but it is suspended
  for as long as some *other* VXI-11 client (Keysight Connection Expert,
  BenchVue) is also connected to this instrument.
* The instrument allows at least 5 concurrent VXI-11 links and a new link can
  be created 0.04 s after the previous one died, so a restarting application
  never has to wait for the old session to be reaped.

A read timeout means different things on the two transports
-----------------------------------------------------------

On the **raw socket** a read timeout is fatal to the session.  The 34461A
leaves the byte stream unusable after a query it never answers: whatever the
instrument eventually emits would be read as the answer to the *next* query,
permanently shifting the conversation by one.  There is no way to ask a stream
whether anything is still in flight, so the session is torn down, a fresh one is
built, and :class:`ScpiTimeout` is raised naming the command that stalled.

On **VXI-11** the same event is an ordinary in-band result and must not tear
anything down.  ``device_read`` answering with error 15 means the RPC
*completed*: the request was framed, the reply was framed, the channel is in
step with itself and nothing is half-read.  All that happened is that the
instrument had nothing to say inside the timeout it was given.  Tearing the
session down anyway would send DESTROY_LINK on the sole link, and this
instrument performs a **device clear** when its last VXI-11 link goes — so
ordinary error recovery would abort the running acquisition and empty reading
memory.  Measured on this unit: a single read timeout handled that way froze
``DATA:POIN?`` at 8 and cleared the readings behind it.  That is the deadman
firing on a non-event, and it is the exact fault this transport exists to
prevent.

Teardown on VXI-11 is therefore reserved for genuine channel corruption — an
RPC reply that never arrives, an xid that does not match, a malformed or
implausible fragment — because those really do leave bytes in flight on the
channel.  Each transport declares which it is in
:attr:`_Transport.read_timeout_desyncs`, and :class:`ScpiLink` reads that rather
than assuming.  The distinction is carried in the exceptions the transports
raise: :class:`TransportTimeout` always means "no data, channel intact", and
:class:`ScpiConnectionError` always means "this session cannot be trusted
again".

Two protections live here rather than in the callers, because IO-DISCIPLINE.md
requires that no caller be able to bypass them:

* **A global ceiling of 40 SCPI operations per second** (rule 5), applied to
  every send across every link by :data:`LIMITER`.  One operation is one
  command on the wire, so an ``R? 4000`` block fetch costs exactly one, however
  many readings it returns.
* **Reconnect backoff of 1, 2, 5, 10, 30 s** (rule 3).  The first rebuild after
  a healthy exchange is immediate, which is what keeps a single stalled query
  from desynchronising the stream; only *repeated* failures are spaced out, so
  a dead instrument can never be hammered with a tight open/close loop.  It was
  hundreds of such cycles that degraded this instrument's Windows CE LAN stack.

  The backoff is armed by *any* genuine failure, not only a refused TCP
  connect: a read timeout on the raw socket, a failed or stalled *write* on
  either transport, and a lost session mid-exchange all arm it.  A
  wedged-but-still-accepting instrument — exactly the degraded state this
  project produced once — answers the SYN, accepts a CREATE_LINK and then
  never completes a DEVICE_WRITE, so the write path is the one it takes;
  arming there is what stops one session close/open per stalled write with no
  spacing between them.  Opening a session also costs the instrument at least
  as much as a command, so it draws a token from the same ceiling.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

# IO-DISCIPLINE.md rule 5.  Sustained ceiling across every link in the process.
MAX_OPS_PER_SECOND = 40

# IO-DISCIPLINE.md rule 3.  Seconds to wait before the 2nd, 3rd, ... rebuild
# attempt; the last value is the cap and repeats forever.
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)

# How long a query may spend draining a previous exchange's leftovers before
# giving up and rebuilding instead.  Only spent when something is actually
# outstanding, and generous next to this instrument's 9 ms round trip.
STALE_DRAIN_SECONDS = 1.0
# The read allowance for one drain step.  One dry read ends the drain, so this
# is what a link with nothing outstanding to say costs, once.
STALE_DRAIN_POLL = 0.2

# The instrument's raw SCPI socket, used by the fallback transport.
RAW_SCPI_PORT = 5025

# Commands verified to hang this instrument's socket (SPEC.md section 2.2).
# Nothing in the application may send these; the /api/scpi expert passthrough
# deliberately bypasses the guard because the user typed them knowingly.
FORBIDDEN = frozenset(
    x.upper()
    for x in (
        "SAMP:SOUR?",
        "SAMP:TIM?",
        "SAMP:COUN:PRET?",
        "TRIG:LEV?",
        "RES:OCOM?",
        "FRES:OCOM?",
        "CONT:THR?",
        "DIOD:THR?",
        "TEMP:UNIT?",
        "VOLT:DC:APER:ENAB?",
        "CALC:SCAL:REF?",
        "CALC:SCAL:GAIN?",
        "CALC:SCAL:OFFS?",
        "CALC:SCAL:PCT?",
        "CALC:SCAL:UNIT?",
        "CALC:SCAL:UNIT:STAT?",
        "DISP:ANN:STAT?",
        "DISP:DIG:MASK?",
        "SYST:PRES?",
        "SYST:LANG?",
        "SYST:IDN?",
        "SYST:HELP:HEAD?",
        "CAP:RES?",
        "FREQ:RES?",
        "PER:RES?",
        "TEMP:RES?",
        # Not in SPEC.md 2.2, but verified on this instrument to hang exactly
        # the same way.  Section 2.1 never lists autozero for these functions;
        # only section 8's summary table wrongly implies FRES has it.
        "FRES:ZERO:AUTO?",
        "VOLT:AC:ZERO:AUTO?",
    )
)


class RateLimiter:
    """A sliding-window ceiling on SCPI operations per second.

    IO-DISCIPLINE.md rule 5 asks for the limit to be enforced *in the transport
    layer so no caller can bypass it*, so this gates :meth:`ScpiLink._send` —
    the single point every command passes through — rather than trusting each
    call site to pace itself.

    The window is one second wide and holds the instants of the last
    :attr:`limit` sends.  A send that would exceed the limit sleeps until the
    oldest event ages out.  Sleeping (rather than raising) is deliberate: a
    caller that is briefly too eager should be slowed down, not failed.
    """

    def __init__(self, limit: int = MAX_OPS_PER_SECOND) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._events: Deque[float] = deque()
        self.throttled_ops = 0
        self.throttled_seconds = 0.0

    def acquire(self, priority: bool = False) -> None:
        """Block until one operation may be sent, then record it.

        *priority* is for the handful of commands that put the instrument back
        into a safe state — ``ABOR``, the trigger restore, ``SYST:LOC``.  Those
        take their token immediately instead of queueing behind whatever is
        being throttled, because a shutdown that waits its turn behind a 33-op
        state read is a shutdown that leaves the meter acquiring.  The event is
        still recorded, so the window keeps accounting for it and the ceiling
        is only ever exceeded by the few operations that end a session.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - 1.0
                while self._events and self._events[0] < cutoff:
                    self._events.popleft()
                if priority or len(self._events) < self.limit:
                    self._events.append(now)
                    return
                # The window is full; wait for its oldest entry to expire.
                wait = (self._events[0] + 1.0) - now
                self.throttled_ops += 1
                self.throttled_seconds += max(wait, 0.0)
            time.sleep(min(max(wait, 0.001), 1.0))

    def rate(self) -> float:
        """Operations sent in the last second, for the UI to display."""
        with self._lock:
            cutoff = time.monotonic() - 1.0
            while self._events and self._events[0] < cutoff:
                self._events.popleft()
            return float(len(self._events))


# The one ceiling for the whole process, shared by every link.
LIMITER = RateLimiter()


class ScpiError(Exception):
    """Any failure while talking to the instrument."""


class ScpiTimeout(ScpiError):
    """A query went unanswered.

    ``link_reset`` says whether the session had to be torn down to recover.  It
    is True on the raw socket, where an unanswered query leaves the byte stream
    shifted by one and only a rebuild repairs it, and False on VXI-11, where the
    timeout arrived as a completed RPC and the link is still up and usable.
    Callers that stop a run because "the link was reset" must test it: on
    VXI-11 the instrument's acquisition is untouched by a read timeout, so
    stopping the run would be both unnecessary and untrue.

    ``rebuilt`` says whether a session is up now.  When it is not, the reconnect
    was *deferred* by the backoff rather than having failed outright, and
    ``detail`` says which.
    """

    def __init__(
        self,
        command: str,
        timeout: float,
        rebuilt: bool,
        detail: str = "",
        link_reset: bool = True,
    ):
        self.command = command
        self.timeout = timeout
        self.rebuilt = rebuilt
        self.link_reset = link_reset
        if link_reset:
            msg = f"No response to {command!r} within {timeout:g} s; link reset"
            if not rebuilt:
                msg += f", and it is not back up yet: {detail}"
        else:
            msg = (
                f"No response to {command!r} within {timeout:g} s; the link is "
                "still up and the instrument was left alone"
            )
            if detail:
                msg += f" ({detail})"
        super().__init__(msg)


class ScpiConnectionError(ScpiError):
    """The session could not be established or was lost mid-exchange."""


class ScpiForbidden(ScpiError):
    """An internal caller tried to send a command known to hang the socket."""


class TransportTimeout(Exception):
    """A transport read produced no bytes, with the session still intact.

    Internal to this module.  It says only "nothing arrived"; whether that is
    survivable is a property of the transport, declared by
    :attr:`_Transport.read_timeout_desyncs` and applied by :class:`ScpiLink`.
    A transport that discovers its session is *not* intact — a reply that never
    came, a bad frame — must raise :class:`ScpiConnectionError` instead, never
    this.
    """


# =========================================================================
# Transports
# =========================================================================


class _Transport:
    """The byte-level session underneath :class:`ScpiLink`.

    Deliberately tiny, because everything that matters — the rate ceiling, the
    reconnect backoff, the never-send guard, the framing repair, the single
    deadline across a block read — lives in :class:`ScpiLink` and must behave
    identically whichever transport is underneath.

    A transport is a *byte stream*: :meth:`send` puts a complete command on the
    wire and :meth:`recv` returns whatever bytes have arrived.  VXI-11 is
    message-oriented rather than a stream, but presenting it as a stream is
    what lets the existing line and block parsers stay untouched.
    """

    #: Human-readable, shown in the UI and in error messages.
    label = "transport"

    #: True when a hard client kill ends the instrument's acquisition by
    #: itself.  Callers use it to decide whether an unbounded ``TRIG:COUN INF``
    #: is safe or whether a finite renewed count is needed as a deadman.
    crash_safe = False

    #: True when a :class:`TransportTimeout` leaves the session unusable and the
    #: only repair is to rebuild it.  A byte stream has to say True — it cannot
    #: tell a query that will never be answered from one that is merely late,
    #: and a late answer read as the next query's reply shifts the conversation
    #: by one for ever.  A message-oriented session says False: the timeout came
    #: back as a completed exchange, so the session is demonstrably in step and
    #: the caller simply has no data yet.
    read_timeout_desyncs = True

    def open(self, connect_timeout: float, read_timeout: float) -> None:
        raise NotImplementedError

    def close(self, graceful: bool = True) -> None:
        """End the session.

        *graceful* is False when the close is itself a failure path — the
        instrument is stalled or the session is already broken.  A transport
        with a polite shutdown handshake must skip it then, because the peer
        that just failed to answer is not going to answer that either, and the
        wait is spent holding :attr:`ScpiLink.lock`.
        """
        raise NotImplementedError

    def send(self, payload: bytes) -> None:
        raise NotImplementedError

    def recv(self, timeout: float) -> bytes:
        raise NotImplementedError


class RawSocketTransport(_Transport):
    """The original transport: one TCP session to port 5025.

    Kept as the fallback for an instrument or a network where VXI-11 is not
    reachable.  It carries no session semantics, so an acquisition started over
    it outlives the client that started it; callers must supply their own
    deadman, which is why :attr:`crash_safe` is False.
    """

    label = "raw socket"
    crash_safe = False
    # A stream: an unanswered query may still be answered later, straight into
    # the next query's reply.  Rebuilding is the only repair, and this
    # behaviour is unchanged from the transport's original form.
    read_timeout_desyncs = True

    def __init__(self, host: str, port: int = RAW_SCPI_PORT):
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None

    def __str__(self) -> str:
        return f"raw socket {self.host}:{self.port}"

    def open(self, connect_timeout: float, read_timeout: float) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=connect_timeout)
        sock.settimeout(read_timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

    def close(self, graceful: bool = True) -> None:
        # There is no shutdown handshake on a raw socket, so a failure close
        # and a deliberate one are the same thing and both are immediate.
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()

    def send(self, payload: bytes) -> None:
        if self._sock is None:
            raise ScpiConnectionError("the raw socket is not open")
        self._sock.sendall(payload)

    def recv(self, timeout: float) -> bytes:
        if self._sock is None:
            raise ScpiConnectionError("the raw socket is not open")
        self._sock.settimeout(timeout)
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout:
            raise TransportTimeout() from None
        if not chunk:
            raise ScpiConnectionError("the instrument closed the link")
        return chunk


# ------------------------------------------------------------------ VXI-11
#
# ONC RPC (RFC 5531) over TCP, carrying the VXI-11 core channel (TCP/IP
# Instrument Protocol Specification VXI-11).  Only the five procedures this
# application needs are implemented: the portmapper lookup, CREATE_LINK,
# DEVICE_WRITE, DEVICE_READ, DEVICE_CLEAR and DESTROY_LINK.  The asynchronous
# abort channel and the interrupt channel are deliberately absent — nothing
# here aborts an in-flight RPC, and SRQ is not used.

_RPC_VERSION = 2
_MSG_CALL = 0
_MSG_REPLY = 1
_REPLY_ACCEPTED = 0
_ACCEPT_SUCCESS = 0
_AUTH_NULL = 0

# reply_stat = MSG_DENIED carries a reject_stat (RFC 5531 section 9): either the
# server does not speak RPC version 2, or it refused our credentials.  Both are
# permanent misconfigurations rather than instrument faults, and saying which
# one saves an entire debugging session.
_RPC_MISMATCH = 0
_AUTH_ERROR = 1
_AUTH_STATS = {
    1: "AUTH_BADCRED",
    2: "AUTH_REJECTEDCRED",
    3: "AUTH_BADVERF",
    4: "AUTH_REJECTEDVERF",
    5: "AUTH_TOOWEAK",
    6: "AUTH_INVALIDRESP",
    7: "AUTH_FAILED",
}
# accept_stat, for a reply that was accepted but not executed.
_ACCEPT_STATS = {
    0: "SUCCESS",
    1: "PROG_UNAVAIL",
    2: "PROG_MISMATCH",
    3: "PROC_UNAVAIL",
    4: "GARBAGE_ARGS",
    5: "SYSTEM_ERR",
}

_PMAP_PROG = 100000
_PMAP_VERS = 2
_PMAP_PORT = 111
_PMAP_GETPORT = 3
_IPPROTO_TCP = 6

_VXI_CORE_PROG = 0x0607AF
_VXI_CORE_VERS = 1
_CREATE_LINK = 10
_DEVICE_WRITE = 11
_DEVICE_READ = 12
_DEVICE_CLEAR = 15
_DESTROY_LINK = 23

# device_write flags
_OP_FLAG_END = 8
# device_read reason bits
_RX_END = 4

# VXI-11 error codes worth naming in a message.
_VXI_ERRORS = {
    0: "no error",
    1: "syntax error",
    3: "device not accessible",
    4: "invalid link identifier",
    5: "parameter error",
    6: "channel not established",
    8: "operation not supported",
    9: "out of resources",
    11: "device locked by another link",
    12: "no lock held by this link",
    15: "I/O timeout",
    17: "I/O error",
    21: "invalid address",
    23: "abort",
    29: "channel already established",
}

# How much of a response to ask for in one DEVICE_READ.  Measured on this unit
# on 2026-08-27: it caps a ``device_read`` reply at 65536 bytes whatever
# ``requestSize`` asks for, so the 277 KB screen frame always costs five round
# trips (65536 x 4 + 15359).  Asking for more than the instrument will ever
# send only misdescribes what happens, so this matches the measured cap.
_VXI_READ_CHUNK = 65536

# Sanity bound on one RPC record.  ``length`` comes from four bytes on the
# wire, so a corrupt or misaligned header can claim up to 2 GB and have the
# client sit there accumulating it.  The largest reply this application can
# provoke is one 65536-byte ``device_read`` payload plus its RPC and VXI-11
# headers, so a megabyte is generous by a factor of sixteen and anything past
# it is corruption, not data.  Applied per fragment and to the reassembled
# record, so a stream of small fragments cannot get round it.
_MAX_RPC_RECORD = 1 << 20

# How long a deliberate DESTROY_LINK may take before the socket is simply
# dropped instead.  Only spent on a clean shutdown; failure paths skip it.
_DESTROY_LINK_TIMEOUT = 2.0

# Pause before reporting "nothing yet" for a zero-length successful read, so a
# caller with seconds of deadline left cannot spin on an instrument that
# answers instantly and empty.  A read that returned error 15 has already spent
# its io_timeout on the instrument and needs no pacing.
_VXI_EMPTY_READ_PAUSE = 0.02


def _xdr_u32(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def _xdr_opaque(data: bytes) -> bytes:
    return _xdr_u32(len(data)) + data + b"\x00" * (-len(data) % 4)


class _XdrReader:
    """Just enough XDR decoding for the replies this module asks for."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def u32(self) -> int:
        end = self._pos + 4
        if end > len(self._data):
            raise ScpiConnectionError("truncated RPC reply")
        value = struct.unpack(">I", self._data[self._pos : end])[0]
        self._pos = end
        return value

    def i32(self) -> int:
        value = self.u32()
        return value - 0x100000000 if value >= 0x80000000 else value

    def opaque(self) -> bytes:
        length = self.u32()
        end = self._pos + length
        if end > len(self._data):
            raise ScpiConnectionError("truncated RPC reply")
        out = self._data[self._pos : end]
        self._pos = end + (-length % 4)
        return out


def _describe_reject(reader: "_XdrReader") -> str:
    """Decode the ``rejected_reply`` body (RFC 5531 section 9) into words.

    A denied reply carries no verifier, so the reject_stat follows reply_stat
    immediately.  Undecoded, an RPC version or program mismatch is
    indistinguishable from a busy instrument, and the two want opposite
    responses from whoever reads the message.
    """
    try:
        stat = reader.u32()
        if stat == _RPC_MISMATCH:
            low, high = reader.u32(), reader.u32()
            return (
                f"RPC_MISMATCH — it speaks RPC versions {low}-{high}, "
                f"this client speaks {_RPC_VERSION}"
            )
        if stat == _AUTH_ERROR:
            why = reader.u32()
            return (
                f"AUTH_ERROR — {_AUTH_STATS.get(why, 'unknown')} ({why}); "
                "it refused the AUTH_NULL credentials"
            )
        return f"unknown reject status {stat}"
    except ScpiConnectionError:
        return "and the rejection itself was truncated"


class _RpcChannel:
    """One TCP connection speaking ONC RPC with record marking.

    Record marking (RFC 5531 section 11) frames each message as a sequence of
    fragments, each prefixed by a 4-byte big-endian header whose top bit marks
    the final fragment and whose low 31 bits give the fragment length.  Both
    directions use it; the instrument sends single-fragment replies but the
    loop below accepts several anyway, because nothing guarantees it will not.
    """

    def __init__(self, host: str, port: int, connect_timeout: float):
        self._sock = socket.create_connection((host, port), timeout=connect_timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._xid = int(time.time()) & 0x7FFFFFFF

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _recv_exact(self, count: int, deadline: float) -> bytes:
        """Read exactly *count* bytes, all of them inside one absolute deadline.

        *deadline* is a :func:`time.monotonic` instant covering the whole read,
        not a per-syscall allowance.  This matters more than it looks: a 277 KB
        screen frame arrives as many TCP segments, and giving each ``recv`` the
        full timeout afresh makes one RPC's worst case ``timeout x segments`` —
        unbounded in practice, and spent holding :attr:`ScpiLink.lock` and the
        instrument gate.  Recomputing the per-syscall timeout from the deadline
        makes the whole call cost at most what the caller asked for.
        """
        out = bytearray()
        while len(out) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeout()
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(count - len(out))
            except socket.timeout:
                raise TransportTimeout() from None
            if not chunk:
                raise ScpiConnectionError("the instrument closed the RPC channel")
            out += chunk
        return bytes(out)

    def call(self, prog: int, vers: int, proc: int, args: bytes, timeout: float) -> bytes:
        """Issue one RPC and return the procedure-specific part of the reply.

        *timeout* bounds the entire exchange — the send, every fragment of the
        reply and every TCP segment within them — because it is turned into one
        absolute deadline here and then honoured by :meth:`_recv_exact`.
        """
        self._xid = (self._xid + 1) & 0x7FFFFFFF
        xid = self._xid
        body = (
            _xdr_u32(xid)
            + _xdr_u32(_MSG_CALL)
            + _xdr_u32(_RPC_VERSION)
            + _xdr_u32(prog)
            + _xdr_u32(vers)
            + _xdr_u32(proc)
            + _xdr_u32(_AUTH_NULL)  # credentials: AUTH_NULL, zero length
            + _xdr_u32(0)
            + _xdr_u32(_AUTH_NULL)  # verifier: AUTH_NULL, zero length
            + _xdr_u32(0)
            + args
        )
        deadline = time.monotonic() + timeout
        self._sock.settimeout(timeout)
        try:
            self._sock.sendall(_xdr_u32(0x80000000 | len(body)) + body)
        except socket.timeout:
            raise TransportTimeout() from None

        # Reassemble the reply record.
        payload = bytearray()
        while True:
            header = struct.unpack(">I", self._recv_exact(4, deadline))[0]
            length = header & 0x7FFFFFFF
            if length > _MAX_RPC_RECORD or len(payload) + length > _MAX_RPC_RECORD:
                # Believing this would mean reading for as long as the peer
                # cares to send.  A length this size is a lost frame boundary,
                # which is exactly the corruption a rebuild is for.
                raise ScpiConnectionError(
                    f"RPC fragment claims {length} bytes, past the "
                    f"{_MAX_RPC_RECORD} byte sanity limit; the channel framing "
                    "is lost"
                )
            payload += self._recv_exact(length, deadline)
            if header & 0x80000000:
                break

        reader = _XdrReader(bytes(payload))
        if reader.u32() != xid:
            # A reply for a different call means this channel is out of step
            # with itself; there is no repairing that in place.
            raise ScpiConnectionError("RPC reply carried the wrong transaction id")
        if reader.u32() != _MSG_REPLY:
            raise ScpiConnectionError("RPC message was not a reply")
        if reader.u32() != _REPLY_ACCEPTED:
            raise ScpiConnectionError(
                "the instrument rejected the RPC call: " + _describe_reject(reader)
            )
        reader.u32()  # verifier flavour
        reader.opaque()  # verifier body
        status = reader.u32()
        if status != _ACCEPT_SUCCESS:
            raise ScpiConnectionError(
                "RPC call failed with accept status "
                f"{_ACCEPT_STATS.get(status, 'unknown')} ({status})"
            )
        return bytes(payload[reader._pos :])


class Vxi11Transport(_Transport):
    """A VXI-11 ``inst0`` session, which the instrument tears down on its own.

    This is the transport that makes a client crash safe.  When the process
    dies the operating system closes its sockets, the instrument's RPC server
    sees the core channel go, destroys the link, and — because it was the last
    link — performs a device clear: the acquisition stops, reading memory is
    emptied and the front panel goes back to free-running.  Nothing in this
    process has to run for that to happen, which is the whole point.
    """

    label = "VXI-11"
    crash_safe = True
    # A read timeout here is a completed RPC, not a broken stream — see the
    # module docstring.  Tearing the link down on one would device-clear the
    # instrument, which is precisely the harm this transport exists to avoid.
    read_timeout_desyncs = False

    def __init__(self, host: str, device: str = "inst0"):
        self.host = host
        self.device = device
        self._channel: Optional[_RpcChannel] = None
        self._link: Optional[int] = None
        self._max_recv = 4096
        self._read_timeout = 10.0
        # Remembered across reconnects so a rebuild costs one TCP connect
        # rather than two; re-discovered if connecting to it ever fails.
        self._core_port: Optional[int] = None

    def __str__(self) -> str:
        port = self._core_port if self._core_port else "?"
        return f"VXI-11 {self.host}:{port} ({self.device})"

    # -------------------------------------------------------------- session

    def _lookup_core_port(self, connect_timeout: float) -> int:
        """Ask the portmapper on 111 where the VXI-11 core channel lives."""
        pmap = _RpcChannel(self.host, _PMAP_PORT, connect_timeout)
        try:
            args = (
                _xdr_u32(_VXI_CORE_PROG)
                + _xdr_u32(_VXI_CORE_VERS)
                + _xdr_u32(_IPPROTO_TCP)
                + _xdr_u32(0)
            )
            try:
                reply = pmap.call(
                    _PMAP_PROG, _PMAP_VERS, _PMAP_GETPORT, args, connect_timeout
                )
            except TransportTimeout:
                # Reported as a connection fault so open()'s stale-port retry
                # and the caller's backoff both see it for what it is.
                raise ScpiConnectionError(
                    f"{self.host} did not answer its portmapper within "
                    f"{connect_timeout:g} s"
                ) from None
        finally:
            pmap.close()
        port = _XdrReader(reply).u32()
        if not port:
            raise ScpiConnectionError(
                f"{self.host} has no VXI-11 core channel registered with its "
                "portmapper"
            )
        return port

    def open(self, connect_timeout: float, read_timeout: float) -> None:
        self._read_timeout = read_timeout
        port = self._core_port
        try:
            if port is None:
                port = self._lookup_core_port(connect_timeout)
            channel = _RpcChannel(self.host, port, connect_timeout)
        except (OSError, ScpiConnectionError):
            if self._core_port is None:
                raise
            # The remembered port went stale — the instrument was restarted and
            # its RPC server came back somewhere else.  Ask once more, then
            # give up and let the caller's backoff space out the next attempt.
            self._core_port = None
            port = self._lookup_core_port(connect_timeout)
            channel = _RpcChannel(self.host, port, connect_timeout)

        try:
            args = (
                _xdr_u32(0)  # clientId; this application keeps only one link
                + _xdr_u32(0)  # lockDevice: never, so other tools stay usable
                + _xdr_u32(0)  # lock_timeout
                + _xdr_opaque(self.device.encode("ascii"))
            )
            reply = self._call(
                channel, _CREATE_LINK, args, connect_timeout, "create_link"
            )
            reader = _XdrReader(reply)
            error = reader.i32()
            if error:
                raise ScpiConnectionError(
                    f"VXI-11 create_link on {self.host} refused: "
                    f"{_VXI_ERRORS.get(error, error)}"
                )
            self._link = reader.i32()
            reader.u32()  # abort port; the async channel is not used
            self._max_recv = max(reader.u32(), 256)
        except Exception:
            channel.close()
            raise
        self._channel = channel
        self._core_port = port

    def close(self, graceful: bool = True) -> None:
        channel, self._channel = self._channel, None
        link, self._link = self._link, None
        if channel is None:
            return
        try:
            if link is not None and graceful:
                # Politeness only: the instrument reaches the same state when
                # the socket simply dies, which is exactly why this transport
                # is safe against a crash.  So it is skipped entirely when the
                # close is a failure path (``graceful=False``) — an instrument
                # that has just stalled on a DEVICE_WRITE will stall on this
                # too, and the wait would be spent holding ScpiLink.lock while
                # buying nothing the socket close does not already deliver.
                # ``call`` bounds the whole exchange by this timeout now, so
                # even the polite path cannot exceed it.
                channel.call(
                    _VXI_CORE_PROG, _VXI_CORE_VERS, _DESTROY_LINK,
                    _xdr_u32(link), _DESTROY_LINK_TIMEOUT,
                )
        except (OSError, ScpiError, TransportTimeout):
            pass
        finally:
            channel.close()

    # ------------------------------------------------------------------ i/o

    def _require(self) -> Tuple[_RpcChannel, int]:
        if self._channel is None or self._link is None:
            raise ScpiConnectionError("the VXI-11 link is not open")
        return self._channel, self._link

    @staticmethod
    def _call(
        channel: _RpcChannel, proc: int, args: bytes, timeout: float, what: str
    ) -> bytes:
        """Make one core-channel call, translating a stalled RPC into a fault.

        A :class:`TransportTimeout` escaping from :meth:`_RpcChannel.call` does
        *not* mean "the instrument had nothing to say": it means the reply to a
        call we already sent never arrived, so it may still arrive and be read
        as the answer to the next one.  That is genuine channel corruption and
        the session cannot be trusted again, so it is reported as such and
        never confused with the in-band ``device_read`` timeout below.
        """
        try:
            return channel.call(_VXI_CORE_PROG, _VXI_CORE_VERS, proc, args, timeout)
        except TransportTimeout:
            raise ScpiConnectionError(
                f"the VXI-11 {what} RPC was not answered within {timeout:g} s; "
                "the channel is out of step"
            ) from None

    def send(self, payload: bytes) -> None:
        channel, link = self._require()
        # A write longer than the device's advertised receive size has to be
        # split, with END set only on the final piece.  Nothing this
        # application sends is anywhere near that, but a silently truncated
        # command would be a nasty way to find out.
        view = memoryview(payload)
        while view:
            piece, view = view[: self._max_recv], view[self._max_recv :]
            flags = _OP_FLAG_END if not view else 0
            args = (
                _xdr_u32(link)
                + _xdr_u32(int(self._read_timeout * 1000))
                + _xdr_u32(0)  # lock_timeout: no locking, see open()
                + _xdr_u32(flags)
                + _xdr_opaque(bytes(piece))
            )
            reply = self._call(
                channel, _DEVICE_WRITE, args, self._read_timeout + 2.0, "write"
            )
            reader = _XdrReader(reply)
            error = reader.i32()
            if error:
                raise ScpiConnectionError(
                    f"VXI-11 write failed: {_VXI_ERRORS.get(error, error)}"
                )
            if reader.u32() != len(piece):
                raise ScpiConnectionError("VXI-11 write was truncated")

    def recv(self, timeout: float) -> bytes:
        """Return whatever bytes the instrument has, or report "nothing yet".

        ``device_read`` blocks on the instrument for its own ``io_timeout``, so
        the socket is given a little longer than that: the reply announcing the
        timeout has to be allowed to arrive, otherwise the RPC channel is left
        with an answer still in flight and has to be thrown away.

        Both ways of saying "nothing arrived" — error 15, and success with an
        empty payload — raise :class:`TransportTimeout`, which on this
        transport is an in-band result the caller's deadline governs.  The link
        is untouched either way: it is still open, still in step, and still the
        only thing standing between a crashed client and an acquisition that
        runs for ever.
        """
        channel, link = self._require()
        io_timeout = max(int(timeout * 1000), 1)
        args = (
            _xdr_u32(link)
            + _xdr_u32(_VXI_READ_CHUNK)
            + _xdr_u32(io_timeout)
            + _xdr_u32(0)  # lock_timeout
            + _xdr_u32(0)  # flags: no termchar, stop on END
            + _xdr_u32(0)  # termChar, unused
        )
        reply = self._call(channel, _DEVICE_READ, args, timeout + 2.0, "read")
        reader = _XdrReader(reply)
        error = reader.i32()
        reader.i32()  # reason bits; the stream model does not need them
        if error and error != 15:
            raise ScpiConnectionError(
                f"VXI-11 read failed: {_VXI_ERRORS.get(error, error)}"
            )
        # VXI-11 permits error 15 to arrive *with* the bytes transferred before
        # the instrument's timeout expired, so the payload is decoded before
        # the error is acted on.  Discarding it would be silent truncation the
        # moment a caller gives ``recv`` less time than its own deadline.
        data = reader.opaque()
        if data:
            return data
        if error == 15:
            # io_timeout — the instrument had nothing to say and spent the whole
            # allowance saying so, which paces this loop by itself.
            raise TransportTimeout()
        # Zero length with no error: not a closed link, just nothing yet.  The
        # instrument answered without waiting, so pace before saying so.
        time.sleep(min(_VXI_EMPTY_READ_PAUSE, max(timeout, 0.0)))
        raise TransportTimeout()

    def device_clear(self, timeout: float = 5.0) -> None:
        """Issue the VXI-11 device clear.

        Not used on any normal path.  It discards the instrument's I/O buffers
        *and* aborts the acquisition, so it is a heavier hammer than ``ABOR``
        and is offered only for a caller that has to reset a link it can no
        longer talk to.
        """
        channel, link = self._require()
        args = (
            _xdr_u32(link)
            + _xdr_u32(0)  # flags
            + _xdr_u32(0)  # lock_timeout
            + _xdr_u32(int(timeout * 1000))
        )
        reply = self._call(channel, _DEVICE_CLEAR, args, timeout + 2.0, "device clear")
        error = _XdrReader(reply).i32()
        if error:
            raise ScpiConnectionError(
                f"VXI-11 device clear failed: {_VXI_ERRORS.get(error, error)}"
            )


def _mnemonic_nodes(head: str) -> List[str]:
    """Split a SCPI header into its colon-separated nodes, upper-cased."""
    return [node for node in head.upper().lstrip(":").split(":") if node]


# The never-send list is written in short form, but SCPI accepts the long form
# of every mnemonic as well: ``SAMPle:SOURce?`` is the same command as
# ``SAMP:SOUR?`` and hangs the socket in exactly the same way.  The long form
# is always the short form with more letters appended to the same node, so a
# node matches when the typed node *starts with* the short form and the header
# has the same number of nodes.  Comparing node counts is what keeps the match
# tight: ``VOLT:DC:APER?`` is legal and must not be caught by the forbidden
# four-node ``VOLT:DC:APER:ENAB?``.
_FORBIDDEN_NODES = tuple(
    tuple(_mnemonic_nodes(entry.rstrip("?"))) for entry in sorted(FORBIDDEN)
)


def check_allowed(command: str) -> None:
    """Raise if *command* is on the never-send list, in short or long form."""
    for piece in command.split(";"):
        head = piece.strip().split(" ")[0].split("\t")[0].strip()
        if not head.endswith("?"):
            # Every entry on the list is a query; the write forms of those
            # nodes are not what hangs the socket.
            continue
        typed = _mnemonic_nodes(head.rstrip("?"))
        if not typed:
            continue
        for forbidden in _FORBIDDEN_NODES:
            if len(typed) != len(forbidden):
                continue
            if all(node.startswith(short) for node, short in zip(typed, forbidden)):
                short_form = ":".join(forbidden) + "?"
                raise ScpiForbidden(
                    f"{head} is {short_form} on the never-send list: "
                    "it hangs this instrument's socket"
                )


class ScpiLink:
    """A single, thread-safe SCPI session.

    *transport* selects the session type:

    ``"auto"``
        Prefer VXI-11 and fall back to the raw socket if the instrument does
        not offer it.  This is the default, because only VXI-11 ends the
        acquisition when this process dies.
    ``"vxi11"`` / ``"socket"``
        Force one or the other, with no fallback.  Forcing the raw socket
        reinstates the old behaviour, in which an acquisition outlives its
        client; :attr:`crash_safe` reports that honestly.
    """

    def __init__(
        self,
        host: str,
        port: int = RAW_SCPI_PORT,
        name: str = "link",
        timeout: float = 10.0,
        connect_timeout: float = 5.0,
        transport: str = "auto",
    ):
        if transport not in ("auto", "vxi11", "socket"):
            raise ValueError(f"unknown transport {transport!r}")
        self.host = host
        self.port = port
        self.name = name
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.transport_choice = transport
        self.lock = threading.RLock()
        self._transport: Optional[_Transport] = None
        # Which kind actually came up.  Under "auto" this is decided at the
        # first successful connect and then kept, so a link does not silently
        # oscillate between a crash-safe session and one that is not.
        self._settled: Optional[str] = None if transport == "auto" else transport
        self._fallback_reason: Optional[str] = None
        self._buf = bytearray()
        # Set when a read gave up on a transport that survives a timeout: the
        # answer may still be coming, so the next exchange drains before it
        # sends rather than reading a late reply as its own.  See
        # :meth:`_discard_stale`.
        self._stale_response = False
        self.last_error: Optional[str] = None
        self.reconnects = 0
        # Reconnect backoff state (rule 3).  ``_attempts`` counts rebuilds
        # since the last healthy exchange; it selects the delay and is reset
        # by :meth:`_note_healthy` the moment the instrument answers again.
        self._attempts = 0
        self._next_attempt_at = 0.0

    # ---------------------------------------------------------------- session

    @property
    def connected(self) -> bool:
        return self._transport is not None

    @property
    def transport_name(self) -> str:
        """What the link is actually using, for the UI to state plainly."""
        if self._transport is not None:
            return self._transport.label
        if self._settled == "vxi11":
            return Vxi11Transport.label
        if self._settled == "socket":
            return RawSocketTransport.label
        return "not connected"

    @property
    def crash_safe(self) -> bool:
        """True when the instrument stops acquiring if this process is killed.

        Callers use this to decide whether ``TRIG:COUN INF`` is safe.  It is
        False until the link has actually come up, so nothing ever assumes the
        protection before it exists.
        """
        return self._transport is not None and self._transport.crash_safe

    @property
    def fallback_reason(self) -> Optional[str]:
        """Why VXI-11 was not used, when the link fell back to the raw socket."""
        return self._fallback_reason

    @property
    def retry_wait(self) -> float:
        """Seconds until a reconnect is allowed, 0.0 when one may go now."""
        return max(0.0, self._next_attempt_at - time.monotonic())

    def _note_healthy(self) -> None:
        """The instrument answered, so the backoff starts again from zero."""
        self._attempts = 0
        self._next_attempt_at = 0.0

    def _arm_backoff(self) -> float:
        """Space out the *next* rebuild after this one; return its delay."""
        delay = RECONNECT_BACKOFF[min(self._attempts, len(RECONNECT_BACKOFF) - 1)]
        self._attempts += 1
        self._next_attempt_at = time.monotonic() + delay
        return delay

    def _build(self, kind: str) -> _Transport:
        if kind == "vxi11":
            return Vxi11Transport(self.host)
        return RawSocketTransport(self.host, self.port)

    def _open_transport(self) -> _Transport:
        """Open a session, applying the auto fallback exactly once."""
        order: List[str]
        if self._settled is not None:
            order = [self._settled]
        else:
            order = ["vxi11", "socket"]
        errors: List[str] = []
        for kind in order:
            candidate = self._build(kind)
            try:
                candidate.open(self.connect_timeout, self.timeout)
            except (OSError, ScpiError, TransportTimeout) as exc:
                try:
                    candidate.close(graceful=False)
                except Exception:
                    pass
                errors.append(f"{candidate}: {exc}")
                continue
            first_error = errors[0] if errors else None
            if self._settled is None:
                self._settled = kind
                if kind == "socket":
                    # Say so once, loudly enough to reach the UI: on this
                    # transport an acquisition survives a crash of this
                    # process, so the caller has to supply its own deadman.
                    self._fallback_reason = (
                        f"VXI-11 was not available ({first_error}); using the "
                        "raw socket, on which an acquisition outlives a crash "
                        "of this application"
                    )
            return candidate
        if not errors:
            raise ScpiConnectionError(f"cannot reach {self.host}")
        # Every candidate failed.  Naming only the first would report an
        # instrument that is off the network as a VXI-11 problem and invite the
        # user to force the raw socket and fail all over again, so say what
        # each one actually did.
        raise ScpiConnectionError("; ".join(errors))

    def connect(self) -> None:
        with self.lock:
            if self._transport is not None:
                return
            wait = self.retry_wait
            if wait > 0.0:
                # Refusing here rather than sleeping keeps the caller
                # responsive; the supervisor retries once the delay is up.
                self.last_error = (
                    f"waiting {wait:.1f} s before the next attempt to reach "
                    f"{self.host}"
                )
                raise ScpiConnectionError(self.last_error)
            # Opening a session costs this instrument's LAN stack at least as
            # much as sending a command, and hundreds of opens are what
            # degraded it, so the open draws a token from the same ceiling
            # rather than slipping past it (rule 5).
            LIMITER.acquire()
            try:
                transport = self._open_transport()
            except (OSError, ScpiError) as exc:
                delay = self._arm_backoff()
                self.last_error = (
                    f"connect to {self.host} failed: {exc}; retrying in {delay:g} s"
                )
                raise ScpiConnectionError(self.last_error) from exc
            self._transport = transport
            self._buf.clear()
            self.last_error = None

    def close(self, graceful: bool = True) -> None:
        """Close the session deliberately; this is not a failure, so the
        backoff is left untouched.

        *graceful* is passed through to the transport: False tells it to skip
        any shutdown handshake, because the close is itself a failure path.
        """
        with self.lock:
            transport, self._transport = self._transport, None
            self._buf.clear()
            self._stale_response = False
            if transport is not None:
                try:
                    transport.close(graceful=graceful)
                except OSError as exc:
                    self.last_error = f"close failed: {exc}"

    def _fail_close(self) -> float:
        """Close after a genuine failure and space out the next attempt.

        Rule 3's backoff exists to stop an open/close loop against an
        instrument that accepts connections and then does not work — the state
        that degraded this unit.  A failed or stalled *write* is exactly that
        instrument, so it has to arm the backoff as surely as a failed connect
        does; without this, a DEVICE_WRITE that stalls on its 12 s timeout
        yields one session teardown and rebuild every 12 s, indefinitely, with
        no spacing at all — and on VXI-11 every one of those cycles device-
        clears the meter as well.  Returns the delay now in force.
        """
        self.close(graceful=False)
        return self._arm_backoff()

    def reconnect(self) -> None:
        """Rebuild the session after a genuine failure, honouring the backoff.

        The first rebuild since the last healthy exchange happens immediately —
        that is what stops one stalled query from leaving the byte stream
        permanently shifted by one.  If it is itself unhealthy the next attempt
        is spaced by :data:`RECONNECT_BACKOFF`, so a dead or wedged instrument
        is never subjected to a tight open/close loop.
        """
        with self.lock:
            # A rebuild is by definition a failure path, so the transport skips
            # its shutdown handshake: whatever just went wrong is not going to
            # answer a polite goodbye either.
            self.close(graceful=False)
            # After a healthy exchange ``_next_attempt_at`` is 0.0, so this
            # rebuild goes straight through.  Should it fail, connect() arms
            # 1 s, then 2, 5, 10 and 30 for each successive attempt.
            self.connect()
            self.reconnects += 1

    def _read_timed_out(self, command: str, timeout: float) -> "ScpiTimeout":
        """Report an unanswered read, tearing the link down only if it must be.

        This is the transport-aware half of the taxonomy.  On a byte stream the
        answer may still be in flight and would be read as the reply to the
        next query, so the session is rebuilt (:meth:`_timeout_reset`) exactly
        as it always was.  On VXI-11 the timeout arrived as a completed RPC:
        the link is in step, so it is left alone and the caller simply learns
        that nothing came.  Rebuilding there would destroy the sole link and
        device-clear the instrument — aborting a running acquisition and
        emptying reading memory as the *recovery* from a non-event.

        What VXI-11 cannot rule out is that the instrument answers late, so the
        link is marked as possibly carrying a stale reply and the next exchange
        drains it before sending (:meth:`_discard_stale`).  That repairs the
        one thing a rebuild would have repaired, without touching the
        acquisition.
        """
        transport = self._transport
        if transport is not None and not transport.read_timeout_desyncs:
            self._buf.clear()
            self._stale_response = True
            return ScpiTimeout(
                command,
                timeout,
                rebuilt=True,
                detail="anything it answers late will be drained before the "
                "next command",
                link_reset=False,
            )
        return self._timeout_reset(command, timeout)

    def _timeout_reset(self, command: str, timeout: float) -> "ScpiTimeout":
        """Rebuild the link after an unanswered query and arm the backoff.

        Only reached on a transport whose timeouts really do desynchronise it;
        :meth:`_read_timed_out` decides.

        The rebuild itself still goes out immediately — that is what stops one
        stalled query from leaving the byte stream permanently shifted by one —
        but a read timeout is a genuine failure, so the *next* rebuild is
        spaced by :data:`RECONNECT_BACKOFF`.  Without this a wedged instrument
        that still completes the TCP handshake gets one socket close/open per
        timeout with no spacing at all, which is the documented mechanism that
        degraded this unit.  :meth:`_note_healthy` clears it again the moment a
        read actually succeeds.
        """
        if self._attempts:
            # Already in failure: this session was opened moments ago and
            # answered nothing at all, so there is no half-read response to
            # repair and nothing to be gained by opening another one straight
            # away.  Drop it and let the backoff decide when the next may go.
            # Without this each timeout costs two opens — one to send on, one
            # to rebuild with — which is the churn this whole mechanism exists
            # to prevent.
            delay = self._fail_close()
            return ScpiTimeout(
                command,
                timeout,
                False,
                f"waiting {delay:g} s before the next attempt to reach {self.host}",
            )
        rebuilt, detail = True, ""
        try:
            self.reconnect()
        except ScpiConnectionError as exc:
            # connect() already armed the backoff on its own failure.
            rebuilt, detail = False, str(exc)
        else:
            self._arm_backoff()
        return ScpiTimeout(command, timeout, rebuilt, detail)

    def _framing_reset(self, message: str) -> ScpiError:
        """Rebuild the link after a framing loss, then describe it.

        SPEC.md section 3 requires a rebuild whenever framing is lost: the rest
        of the response is still in flight, and a 277 KB screen frame read as
        the answer to the next query is how the conversation ends up shifted by
        one.  Clearing the buffer is not enough — the bytes are on the wire,
        not in it.
        """
        detail = "; the link was rebuilt"
        try:
            self.reconnect()
        except ScpiConnectionError as exc:
            detail = f"; the link is not back up yet: {exc}"
        else:
            self._arm_backoff()
        return ScpiError(message + detail)

    def _ensure(self) -> _Transport:
        if self._transport is None:
            self.connect()
        assert self._transport is not None
        return self._transport

    # ------------------------------------------------------------------- i/o

    def _send(self, command: str, priority: bool = False) -> None:
        """The single point every SCPI operation passes through.

        The global rate ceiling is applied here precisely because it is the
        one chokepoint: a caller cannot reach the instrument without going
        past it, so rule 5 cannot be bypassed by adding a new call site.  One
        send is one operation, which is why an ``R? 4000`` block costs the
        same single token as a ``*IDN?``.

        *priority* is reserved for the commands that hand the instrument back
        (``ABOR``, the trigger restore, ``SYST:LOC``); see
        :meth:`RateLimiter.acquire`.
        """
        transport = self._ensure()
        payload = command.encode("ascii", "strict") + b"\n"
        LIMITER.acquire(priority)
        try:
            transport.send(payload)
        except TransportTimeout:
            delay = self._fail_close()
            raise ScpiConnectionError(
                f"send of {command!r} timed out; retrying in {delay:g} s"
            ) from None
        except (OSError, ScpiConnectionError) as exc:
            delay = self._fail_close()
            raise ScpiConnectionError(
                f"send of {command!r} failed: {exc}; retrying in {delay:g} s"
            ) from exc

    def _recv_chunk(self, transport: _Transport, command: str, timeout: float) -> bytes:
        """One read, translating a dry one into whatever this transport means.

        On a transport that survives a timeout this returns no bytes at all and
        lets the caller's own deadline decide when to give up — which is what
        makes ``device_read`` returning "nothing yet" an ordinary, cheap event
        instead of a session teardown.
        """
        try:
            return transport.recv(timeout)
        except TransportTimeout:
            if transport.read_timeout_desyncs:
                raise self._timeout_reset(command, timeout) from None
            return b""
        except ScpiConnectionError as exc:
            delay = self._fail_close()
            raise ScpiConnectionError(
                f"read after {command!r} failed: {exc}; retrying in {delay:g} s"
            ) from exc
        except OSError as exc:
            delay = self._fail_close()
            raise ScpiConnectionError(
                f"read after {command!r} failed: {exc}; retrying in {delay:g} s"
            ) from exc

    def _read_line(self, command: str, timeout: float) -> bytes:
        transport = self._ensure()
        deadline = time.monotonic() + timeout
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                self._note_healthy()
                return line.rstrip(b"\r")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._buf.clear()
                raise self._read_timed_out(command, timeout)
            self._buf += self._recv_chunk(transport, command, remaining)

    def _read_exact(
        self,
        count: int,
        command: str,
        timeout: float,
        deadline: Optional[float] = None,
    ) -> bytes:
        """Read exactly *count* bytes.

        *deadline* is an absolute :func:`time.monotonic` instant.  Passing one
        lets a multi-part exchange such as :meth:`query_block` spend a single
        *timeout* budget across all of its reads instead of granting the full
        timeout to each of them.
        """
        transport = self._ensure()
        if deadline is None:
            deadline = time.monotonic() + timeout
        while len(self._buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._buf.clear()
                raise self._read_timed_out(command, timeout)
            self._buf += self._recv_chunk(transport, command, remaining)
        out = bytes(self._buf[:count])
        del self._buf[:count]
        self._note_healthy()
        return out

    def _discard_stale(self) -> None:
        """Clear a leftover response before sending the next command.

        Bytes sitting in the buffer, or a read that gave up while an answer
        might still have been coming, both mean the same thing: part of a
        previous exchange is unaccounted for.  Clearing the buffer alone was
        never enough, because the rest of that response is on the wire rather
        than in it, and reading it as the answer to the *next* command is
        exactly the shifted-by-one conversation this module exists to prevent.

        So the wire is actually drained.  This runs before the new command goes
        out, so anything readable here is by definition stale, and it stops at
        the first read that comes back dry.  It is bounded by
        :data:`STALE_DRAIN_SECONDS`, costs nothing on a healthy link because it
        only runs when something is outstanding, and — unlike a rebuild — it
        repairs the framing without a session teardown, which on VXI-11 would
        device-clear the instrument.
        """
        if not self._buf and not self._stale_response:
            return
        self._buf.clear()
        self._stale_response = False
        transport = self._transport
        if transport is None:
            return
        deadline = time.monotonic() + STALE_DRAIN_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Still arriving after the whole budget: this is not a late
                # reply but a talkative instrument, and the framing cannot be
                # trusted.  Rebuild, as SPEC.md section 3 requires.
                raise self._framing_reset(
                    "the instrument was still sending a stale response after "
                    f"{STALE_DRAIN_SECONDS:g} s"
                )
            try:
                if not transport.recv(min(remaining, STALE_DRAIN_POLL)):
                    break
            except TransportTimeout:
                break  # nothing more to come; the link is clean again
            except (OSError, ScpiConnectionError) as exc:
                delay = self._fail_close()
                raise ScpiConnectionError(
                    f"clearing a stale response failed: {exc}; "
                    f"retrying in {delay:g} s"
                ) from exc

    # --------------------------------------------------------------- public

    def write(
        self, command: str, guard: bool = True, priority: bool = False
    ) -> None:
        """Send a command that produces no response.

        The backoff is deliberately *not* cleared here.  A send succeeds
        against a wedged instrument as readily as against a healthy one — the
        bytes only have to reach the kernel's send buffer — so treating a write
        as proof of health is what let a failing link be rebuilt once per
        timeout with no spacing.  Only an answer that actually came back
        (:meth:`_note_healthy`, called from the read paths) proves anything.
        """
        if guard:
            check_allowed(command)
        with self.lock:
            self._send(command, priority)

    def query(
        self, command: str, timeout: Optional[float] = None, guard: bool = True
    ) -> str:
        """Send *command* and return its single-line response, stripped."""
        if guard:
            check_allowed(command)
        tmo = self.timeout if timeout is None else timeout
        with self.lock:
            # Anything left over from a previous exchange is read off the wire
            # here, not merely dropped from the buffer.
            self._ensure()
            self._discard_stale()
            self._send(command)
            return self._read_line(command, tmo).decode("latin-1").strip()

    def query_block(
        self, command: str, timeout: Optional[float] = None, guard: bool = True
    ) -> bytes:
        """Send *command* and return an IEEE-488.2 definite-length block body."""
        if guard:
            check_allowed(command)
        tmo = self.timeout if timeout is None else timeout
        with self.lock:
            self._ensure()
            self._discard_stale()
            self._send(command)
            # One deadline for the whole block: header, length, body and
            # terminator together may not exceed *tmo*.
            deadline = time.monotonic() + tmo
            # Every failure below happens with the rest of the response still
            # in flight, so each one rebuilds the link rather than leaving the
            # stream shifted by one (SPEC.md section 3).
            head = self._read_exact(2, command, tmo, deadline)
            if head[:1] != b"#":
                raise self._framing_reset(
                    f"{command!r}: expected a definite-length block, got {head!r}"
                )
            try:
                ndigits = int(head[1:2])
            except ValueError as exc:
                raise self._framing_reset(
                    f"{command!r}: bad block header {head!r}"
                ) from exc
            if ndigits == 0:
                raise self._framing_reset(
                    f"{command!r}: indefinite-length blocks not supported"
                )
            try:
                length = int(self._read_exact(ndigits, command, tmo, deadline))
            except ValueError as exc:
                raise self._framing_reset(
                    f"{command!r}: bad block length field"
                ) from exc
            body = self._read_exact(length, command, tmo, deadline)
            # The instrument terminates the block with a newline.
            trailer = self._read_exact(1, command, tmo, deadline)
            if trailer not in (b"\n", b"\r"):
                self._buf[:0] = trailer
            return body

    # ------------------------------------------------------- typed helpers

    def query_float(self, command: str, timeout: Optional[float] = None) -> float:
        raw = self.query(command, timeout)
        try:
            return float(raw)
        except ValueError as exc:
            raise ScpiError(f"{command!r}: expected a number, got {raw!r}") from exc

    def query_int(self, command: str, timeout: Optional[float] = None) -> int:
        return int(round(self.query_float(command, timeout)))

    def query_bool(self, command: str, timeout: Optional[float] = None) -> bool:
        raw = self.query(command, timeout).strip().upper()
        if raw in ("1", "ON", "+1"):
            return True
        if raw in ("0", "OFF", "+0"):
            return False
        raise ScpiError(f"{command!r}: expected a boolean, got {raw!r}")

    def query_str(self, command: str, timeout: Optional[float] = None) -> str:
        raw = self.query(command, timeout)
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            return raw[1:-1]
        return raw

    def query_floats(
        self, command: str, timeout: Optional[float] = None
    ) -> List[float]:
        raw = self.query(command, timeout)
        if not raw:
            return []
        try:
            return [float(part) for part in raw.split(",")]
        except ValueError as exc:
            raise ScpiError(f"{command!r}: expected CSV numbers, got {raw!r}") from exc

    @staticmethod
    def parse_block_floats(body: bytes) -> List[float]:
        """Parse the comma-separated float payload returned by ``R?``."""
        text = body.decode("latin-1").strip()
        if not text:
            return []
        return [float(part) for part in text.split(",") if part.strip()]


def boolean(value: object) -> str:
    """Render a Python truthiness as the SCPI literal the 34461A expects."""
    if isinstance(value, str):
        token = value.strip().upper()
        if token in ("1", "ON", "TRUE", "YES"):
            return "ON"
        if token in ("0", "OFF", "FALSE", "NO"):
            return "OFF"
        raise ValueError(f"not a boolean: {value!r}")
    return "ON" if value else "OFF"
