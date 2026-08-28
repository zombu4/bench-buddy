#!/usr/bin/env python3
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

"""Characterise an unknown LAN test instrument by measuring it, not guessing it.

This generalises the method that produced ``SPEC.md`` sections 1 and 2 for the
Keysight 34461A: point it at an address, give it a list of *candidate* commands,
and it reports which of them the instrument actually answers, which hang it,
what transports exist, whether the instrument cleans up after a dead client,
how fast it measures, and what its ranges are.

Nothing here assumes the target is a 34461A, or a Keithley, or even a DMM.
Every command it sends comes from the selected profile's candidate list, and
``--dry-run`` prints that list without opening a socket.

    python tools/probe_instrument.py 192.0.2.50 --profile keysight-34461a
    python tools/probe_instrument.py 192.0.2.50 --profile keithley-dmm6500
    python tools/probe_instrument.py 192.0.2.50 --candidates my_list.json
    python tools/probe_instrument.py 192.0.2.50 --dry-run

Output: ``<out>/probe-<host>-<timestamp>.json`` (machine readable, everything)
and ``<out>/probe-<host>-<timestamp>.md`` (shaped like SPEC.md sections 1-2, so
it can become a driver table directly).

Safety rules this file implements, because it runs against instruments nobody
has characterised (see ``IO-DISCIPLINE.md`` for why each of these exists):

* Short timeouts.  A query that does not answer within ``--timeout`` seconds is
  a *hang*, and the link is torn down and rebuilt before the next candidate, so
  one poisoned command does not invalidate the rest of the run.
* A hard ceiling on SCPI operations per second, enforced in the link, and a
  hard ceiling on total run seconds.  No tight loops, no unattended soak.
* A health canary between phases -- a small HTTP request to the instrument's own
  web server, compared against a baseline measured before any test traffic.  If
  the instrument starts degrading, the whole run aborts and says so.
* Teardown runs on success, on exception and on Ctrl-C: abort the acquisition,
  restore the trigger and function configuration that was in force when the
  probe started, and hand the instrument back in local and free-running.
* Commands listed in a profile's ``never_send`` are refused by the link itself.

Requires only the standard library.  Python 3.14 on Windows is the target.

JSON report schema (top level keys):

    meta          host, started, finished, argv, profile, tool version
    transports    per-transport reachability, and which one was used
    identity      raw *IDN? and friends, parsed model/serial/firmware, language
    canary        baseline and per-phase samples, and whether it degraded
    commands      one record per candidate: answered / timed out / reply / error
    functions     per function: selected ok?, range min/max, derived range list,
                  measured readback of each range, nplc/aperture limits
    throughput    readings per second at each integration point tried
    screen        each capture candidate: bytes, seconds, sniffed format
    crash_safety  per transport: did the acquisition survive a hard client kill
    teardown      what was restored and whether each step worked
    aborted       null, or the reason the run stopped early
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

TOOL_VERSION = "1.0"

# --------------------------------------------------------------------------
# Safety limits.  These are deliberately stricter than the application's own
# limits (IO-DISCIPLINE.md rule 5 allows 40 ops/s); a probe is talking to an
# instrument whose LAN stack nobody has measured yet.
# --------------------------------------------------------------------------

DEFAULT_TIMEOUT = 3.0           # seconds a candidate query gets before it is a hang
CONNECT_TIMEOUT = 4.0           # seconds to establish any transport
MAX_OPS_PER_SECOND = 20         # link-level ceiling, no caller can bypass it
PHASE_GAP = 0.4                 # quiet time between phases
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0)
MAX_CONSECUTIVE_TIMEOUTS = 6    # a wall of hangs means the link is gone, not the command
MAX_TOTAL_TIMEOUTS = 80
DEFAULT_MAX_SECONDS = 900       # whole-run budget
REPLY_CLIP = 400                # characters of a reply kept in the report

RAW_SCPI_PORT = 5025
TELNET_PORT = 23
VXI11_DIRECT_PORT = 1024        # Keithley documents the core channel here
HISLIP_PORT = 4880
DST_PORT = 5030                 # Keithley "dead socket termination"
HTTP_PORT = 80
PORTMAP_PORT = 111


class ProbeTimeout(Exception):
    """A command was sent and nothing came back inside its deadline."""

    def __init__(self, command: str, timeout: float):
        super().__init__(f"no answer to {command!r} within {timeout:g} s")
        self.command = command
        self.timeout = timeout


class ProbeLinkError(Exception):
    """The transport itself failed -- not the instrument declining to answer."""


class ProbeForbidden(Exception):
    """The profile's never-send list refused this command."""


class ProbeAborted(Exception):
    """The run stopped early: the canary degraded, or a budget ran out."""


class TransportTimeout(Exception):
    """Nothing to read yet.  Internal to the transports."""


# ==========================================================================
# Transports
# ==========================================================================


class Transport:
    """One session with the instrument.  Byte-level; no SCPI knowledge."""

    label = "transport"
    kind = "none"
    #: What the project needs to know: does the instrument itself clean up when
    #: this session dies?  Only ``crash_safety`` may set this to a fact; here it
    #: is only what the transport is *expected* to do, and it is never reported
    #: as measured.
    expected_crash_safe = False

    def open(self, connect_timeout: float, read_timeout: float) -> None:
        raise NotImplementedError

    def close(self, graceful: bool = True) -> None:
        raise NotImplementedError

    def send(self, payload: bytes) -> None:
        raise NotImplementedError

    def recv(self, timeout: float) -> bytes:
        raise NotImplementedError

    def describe(self) -> str:
        return self.label


class RawSocketTransport(Transport):
    """A bare TCP stream on the instrument's raw SCPI port.

    No session semantics whatever: the instrument cannot tell a dead client
    from a quiet one, so nothing is cleaned up when the process dies.  That is
    exactly the property ``crash_safety`` measures rather than assumes.
    """

    kind = "raw"
    expected_crash_safe = False

    def __init__(self, host: str, port: int = RAW_SCPI_PORT):
        self.host = host
        self.port = port
        self.label = f"raw socket {host}:{port}"
        self._sock: Optional[socket.socket] = None

    def open(self, connect_timeout: float, read_timeout: float) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=connect_timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

    def close(self, graceful: bool = True) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def send(self, payload: bytes) -> None:
        if self._sock is None:
            raise ProbeLinkError("raw socket is not open")
        try:
            self._sock.sendall(payload)
        except OSError as exc:
            raise ProbeLinkError(f"raw socket send failed: {exc}") from exc

    def recv(self, timeout: float) -> bytes:
        if self._sock is None:
            raise ProbeLinkError("raw socket is not open")
        self._sock.settimeout(max(timeout, 0.01))
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout:
            raise TransportTimeout() from None
        except OSError as exc:
            raise ProbeLinkError(f"raw socket read failed: {exc}") from exc
        if not chunk:
            raise ProbeLinkError("the instrument closed the raw socket")
        return chunk


# ---------------------------------------------------------------- VXI-11
#
# ONC RPC (RFC 5531) over TCP carrying the VXI-11 core channel.  Only the
# procedures a probe needs are implemented: the portmapper lookup, CREATE_LINK,
# DEVICE_WRITE, DEVICE_READ, DEVICE_CLEAR and DESTROY_LINK.  The asynchronous
# abort channel and the interrupt channel are deliberately absent.

_RPC_VERSION = 2
_MSG_CALL = 0
_MSG_REPLY = 1
_REPLY_ACCEPTED = 0
_ACCEPT_SUCCESS = 0
_AUTH_NULL = 0

_PMAP_PROG = 100000
_PMAP_VERS = 2
_PMAP_GETPORT = 3
_IPPROTO_TCP_NUM = 6

_VXI_CORE_PROG = 0x0607AF
_VXI_CORE_VERS = 1
_CREATE_LINK = 10
_DEVICE_WRITE = 11
_DEVICE_READ = 12
_DEVICE_CLEAR = 15
_DESTROY_LINK = 23

_OP_FLAG_END = 8
_VXI_READ_CHUNK = 65536
_MAX_RPC_RECORD = 1 << 21

_VXI_ERRORS = {
    0: "no error", 1: "syntax error", 3: "device not accessible",
    4: "invalid link identifier", 5: "parameter error",
    6: "channel not established", 8: "operation not supported",
    9: "out of resources", 11: "device locked by another link",
    12: "no lock held by this link", 15: "I/O timeout", 17: "I/O error",
    21: "invalid address", 23: "abort", 29: "channel already established",
}


def _u32(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def _opaque(data: bytes) -> bytes:
    return _u32(len(data)) + data + b"\x00" * (-len(data) % 4)


class _Xdr:
    def __init__(self, data: bytes):
        self._data = data
        self.pos = 0

    def u32(self) -> int:
        end = self.pos + 4
        if end > len(self._data):
            raise ProbeLinkError("truncated RPC reply")
        value = struct.unpack(">I", self._data[self.pos:end])[0]
        self.pos = end
        return value

    def i32(self) -> int:
        value = self.u32()
        return value - 0x100000000 if value >= 0x80000000 else value

    def opaque(self) -> bytes:
        length = self.u32()
        end = self.pos + length
        if end > len(self._data):
            raise ProbeLinkError("truncated RPC reply")
        out = self._data[self.pos:end]
        self.pos = end + (-length % 4)
        return out


class _RpcChannel:
    """One TCP connection speaking ONC RPC with record marking."""

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
            except OSError as exc:
                raise ProbeLinkError(f"RPC read failed: {exc}") from exc
            if not chunk:
                raise ProbeLinkError("the instrument closed the RPC channel")
            out += chunk
        return bytes(out)

    def call(self, prog: int, vers: int, proc: int, args: bytes, timeout: float) -> bytes:
        self._xid = (self._xid + 1) & 0x7FFFFFFF
        xid = self._xid
        body = (
            _u32(xid) + _u32(_MSG_CALL) + _u32(_RPC_VERSION)
            + _u32(prog) + _u32(vers) + _u32(proc)
            + _u32(_AUTH_NULL) + _u32(0)      # credentials
            + _u32(_AUTH_NULL) + _u32(0)      # verifier
            + args
        )
        deadline = time.monotonic() + timeout
        self._sock.settimeout(timeout)
        try:
            self._sock.sendall(_u32(0x80000000 | len(body)) + body)
        except socket.timeout:
            raise TransportTimeout() from None
        except OSError as exc:
            raise ProbeLinkError(f"RPC send failed: {exc}") from exc

        payload = bytearray()
        while True:
            header = struct.unpack(">I", self._recv_exact(4, deadline))[0]
            length = header & 0x7FFFFFFF
            if length > _MAX_RPC_RECORD or len(payload) + length > _MAX_RPC_RECORD:
                raise ProbeLinkError(
                    f"RPC fragment claims {length} bytes; the framing is lost"
                )
            payload += self._recv_exact(length, deadline)
            if header & 0x80000000:
                break

        reader = _Xdr(bytes(payload))
        if reader.u32() != xid:
            raise ProbeLinkError("RPC reply carried the wrong transaction id")
        if reader.u32() != _MSG_REPLY:
            raise ProbeLinkError("RPC message was not a reply")
        if reader.u32() != _REPLY_ACCEPTED:
            raise ProbeLinkError("the instrument rejected the RPC call")
        reader.u32()
        reader.opaque()
        status = reader.u32()
        if status != _ACCEPT_SUCCESS:
            raise ProbeLinkError(f"RPC call failed with accept status {status}")
        return bytes(payload[reader.pos:])


class Vxi11Transport(Transport):
    """A VXI-11 ``inst0`` session.

    On some instruments (measured true for the 34461A, unknown for anything
    else) the RPC server performs a device clear when the last link is
    destroyed, which the operating system does for us when the process dies.
    That is the crash-safety property ``crash_safety`` exists to measure --
    ``expected_crash_safe`` below is a hypothesis, never a report.
    """

    kind = "vxi11"
    expected_crash_safe = True

    def __init__(self, host: str, device: str = "inst0", port: Optional[int] = None):
        self.host = host
        self.device = device
        self.label = f"VXI-11 {host} ({device})"
        self._channel: Optional[_RpcChannel] = None
        self._link: Optional[int] = None
        self._max_recv = 4096
        self._read_timeout = DEFAULT_TIMEOUT
        self._core_port = port
        #: How the core port was found, for the report.
        self.port_source = "given" if port else "unknown"

    def describe(self) -> str:
        port = self._core_port or "?"
        return f"VXI-11 {self.host}:{port} ({self.device})"

    def lookup_core_port(self, connect_timeout: float) -> int:
        """Ask the portmapper on 111, then fall back to the documented 1024.

        Keithley's manuals list the VXI-11 core channel on port 1024 directly;
        Keysight's is found through the portmapper.  Try the portmapper first
        because it is the answer that is right by construction, then try 1024
        because some instruments do not run rpcbind at all.
        """
        try:
            pmap = _RpcChannel(self.host, PORTMAP_PORT, connect_timeout)
        except OSError as exc:
            last = exc
        else:
            try:
                args = (_u32(_VXI_CORE_PROG) + _u32(_VXI_CORE_VERS)
                        + _u32(_IPPROTO_TCP_NUM) + _u32(0))
                reply = pmap.call(_PMAP_PROG, _PMAP_VERS, _PMAP_GETPORT,
                                  args, connect_timeout)
                port = _Xdr(reply).u32()
                if port:
                    self.port_source = "portmapper"
                    return port
                last = ProbeLinkError("portmapper has no VXI-11 core registered")
            except (TransportTimeout, ProbeLinkError, OSError) as exc:
                last = exc
            finally:
                pmap.close()
        # Fall back to the port Keithley documents.
        try:
            probe = socket.create_connection((self.host, VXI11_DIRECT_PORT),
                                             timeout=connect_timeout)
            probe.close()
        except OSError:
            raise ProbeLinkError(f"no VXI-11 core channel found: {last}") from None
        self.port_source = f"direct :{VXI11_DIRECT_PORT} (portmapper: {last})"
        return VXI11_DIRECT_PORT

    def open(self, connect_timeout: float, read_timeout: float) -> None:
        self._read_timeout = read_timeout
        port = self._core_port or self.lookup_core_port(connect_timeout)
        try:
            channel = _RpcChannel(self.host, port, connect_timeout)
        except OSError as exc:
            raise ProbeLinkError(f"VXI-11 connect to :{port} failed: {exc}") from exc
        try:
            args = (_u32(0) + _u32(0) + _u32(0)
                    + _opaque(self.device.encode("ascii")))
            reply = self._call(channel, _CREATE_LINK, args, connect_timeout, "create_link")
            reader = _Xdr(reply)
            error = reader.i32()
            if error:
                raise ProbeLinkError(
                    f"VXI-11 create_link refused: {_VXI_ERRORS.get(error, error)}"
                )
            self._link = reader.i32()
            reader.u32()                       # abort port; unused
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
                channel.call(_VXI_CORE_PROG, _VXI_CORE_VERS, _DESTROY_LINK,
                             _u32(link), 2.0)
        except (OSError, ProbeLinkError, TransportTimeout):
            pass
        finally:
            channel.close()

    @staticmethod
    def _call(channel: _RpcChannel, proc: int, args: bytes,
              timeout: float, what: str) -> bytes:
        try:
            return channel.call(_VXI_CORE_PROG, _VXI_CORE_VERS, proc, args, timeout)
        except TransportTimeout:
            raise ProbeLinkError(
                f"the VXI-11 {what} RPC was not answered within {timeout:g} s"
            ) from None

    def _require(self) -> Tuple[_RpcChannel, int]:
        if self._channel is None or self._link is None:
            raise ProbeLinkError("the VXI-11 link is not open")
        return self._channel, self._link

    def send(self, payload: bytes) -> None:
        channel, link = self._require()
        view = memoryview(payload)
        while view:
            piece, view = view[: self._max_recv], view[self._max_recv:]
            flags = _OP_FLAG_END if not view else 0
            args = (_u32(link) + _u32(int(self._read_timeout * 1000)) + _u32(0)
                    + _u32(flags) + _opaque(bytes(piece)))
            reply = self._call(channel, _DEVICE_WRITE, args,
                               self._read_timeout + 2.0, "write")
            reader = _Xdr(reply)
            error = reader.i32()
            if error:
                raise ProbeLinkError(
                    f"VXI-11 write failed: {_VXI_ERRORS.get(error, error)}"
                )
            if reader.u32() != len(piece):
                raise ProbeLinkError("VXI-11 write was truncated")

    def recv(self, timeout: float) -> bytes:
        channel, link = self._require()
        io_timeout = max(int(timeout * 1000), 1)
        args = (_u32(link) + _u32(_VXI_READ_CHUNK) + _u32(io_timeout)
                + _u32(0) + _u32(0) + _u32(0))
        reply = self._call(channel, _DEVICE_READ, args, timeout + 2.0, "read")
        reader = _Xdr(reply)
        error = reader.i32()
        reader.i32()                           # reason bits
        if error and error != 15:
            raise ProbeLinkError(
                f"VXI-11 read failed: {_VXI_ERRORS.get(error, error)}"
            )
        data = reader.opaque()
        if data:
            return data
        if error != 15:
            time.sleep(min(0.02, max(timeout, 0.0)))
        raise TransportTimeout()

    def device_clear(self, timeout: float = 5.0) -> None:
        channel, link = self._require()
        args = _u32(link) + _u32(0) + _u32(0) + _u32(int(timeout * 1000))
        reply = self._call(channel, _DEVICE_CLEAR, args, timeout + 2.0, "device clear")
        error = _Xdr(reply).i32()
        if error:
            raise ProbeLinkError(
                f"VXI-11 device clear failed: {_VXI_ERRORS.get(error, error)}"
            )


def hislip_probe(host: str, sub_address: str = "hislip0",
                 timeout: float = CONNECT_TIMEOUT) -> Dict[str, Any]:
    """Perform only the HiSLIP Initialize exchange and report what came back.

    This is an *availability* probe, not a transport.  It opens the synchronous
    channel, sends one Initialize message, reads the InitializeResponse and
    closes.  That is enough to say whether HiSLIP exists, what protocol version
    the instrument negotiates and what session id it hands out -- without
    implementing a protocol this tool would only use to answer that question.
    """
    result: Dict[str, Any] = {
        "available": False, "sub_address": sub_address, "detail": None,
        "protocol_version": None, "session_id": None, "overlap_preferred": None,
    }
    header = b"HS" + bytes([0, 0]) + struct.pack(">HH", 0x0100, 0) \
        + struct.pack(">Q", len(sub_address))
    try:
        sock = socket.create_connection((host, HISLIP_PORT), timeout=timeout)
    except OSError as exc:
        result["detail"] = f"no listener on {HISLIP_PORT}: {exc}"
        return result
    try:
        sock.settimeout(timeout)
        sock.sendall(header + sub_address.encode("ascii"))
        reply = b""
        while len(reply) < 16:
            chunk = sock.recv(16 - len(reply))
            if not chunk:
                break
            reply += chunk
        if len(reply) < 16 or reply[:2] != b"HS":
            result["detail"] = f"no HiSLIP prologue in reply {reply[:16]!r}"
            return result
        msg_type = reply[2]
        control = reply[3]
        version, session = struct.unpack(">HH", reply[4:8])
        if msg_type != 1:
            result["detail"] = f"expected InitializeResponse (1), got type {msg_type}"
            return result
        result.update({
            "available": True,
            "protocol_version": f"{version >> 8}.{version & 0xFF}",
            "session_id": session,
            "overlap_preferred": bool(control & 0x01),
            "detail": "Initialize exchange completed; full HiSLIP messaging is "
                      "not implemented by this probe",
        })
    except (OSError, socket.timeout) as exc:
        result["detail"] = f"HiSLIP Initialize failed: {exc}"
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return result


def tcp_probe(host: str, port: int, timeout: float = 2.0) -> Dict[str, Any]:
    """Can we open and immediately close a TCP session on *port*?"""
    started = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return {"port": port, "open": False, "ms": None, "detail": str(exc)}
    sock.close()
    return {"port": port, "open": True,
            "ms": round((time.monotonic() - started) * 1000, 1), "detail": None}


# ==========================================================================
# Health canary
# ==========================================================================


class Canary:
    """The early warning that the instrument's LAN stack is being stressed.

    The 34461A taught this lesson: its index page always takes ~5 s and that is
    *normal*, while a small 404 on the same server answers in 25 ms and is the
    sensitive signal.  So the canary is a deliberately tiny request, judged
    against its own baseline measured before any test traffic -- never against
    an expectation of "fast", which raises a false alarm on a healthy meter.
    """

    #: A path chosen to be absent, so the answer is a small 404 rather than a
    #: page the instrument has to render.
    PATH = "/probe-canary-does-not-exist"

    def __init__(self, host: str, enabled: bool = True):
        self.host = host
        self.enabled = enabled
        self.baseline_ms: Optional[float] = None
        self.samples: List[Dict[str, Any]] = []
        self.mode = "none"
        self.degraded_reason: Optional[str] = None
        self._consecutive_bad = 0

    # -- one measurement -------------------------------------------------

    def _http_sample(self) -> Tuple[Optional[float], Optional[str]]:
        url = f"http://{self.host}{self.PATH}"
        request = urllib.request.Request(url, method="GET",
                                         headers={"Connection": "close"})
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=6.0) as response:
                response.read(2048)
        except urllib.error.HTTPError:
            pass                                # a 404 is the expected answer
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return None, str(exc)
        return round((time.monotonic() - started) * 1000, 1), None

    def _tcp_sample(self) -> Tuple[Optional[float], Optional[str]]:
        result = tcp_probe(self.host, HTTP_PORT, timeout=6.0)
        if result["open"]:
            return result["ms"], None
        return None, result["detail"]

    def _sample(self) -> Tuple[Optional[float], Optional[str]]:
        if self.mode == "http":
            return self._http_sample()
        if self.mode == "tcp":
            return self._tcp_sample()
        return None, "no canary available"

    # -- lifecycle -------------------------------------------------------

    def establish(self) -> None:
        """Decide what kind of canary this instrument can support, and baseline it."""
        if not self.enabled:
            self.mode = "disabled"
            return
        self.mode = "http"
        readings: List[float] = []
        for _ in range(3):
            value, error = self._http_sample()
            if value is not None:
                readings.append(value)
            time.sleep(0.2)
        if not readings:
            self.mode = "tcp"
            for _ in range(3):
                value, error = self._tcp_sample()
                if value is not None:
                    readings.append(value)
                time.sleep(0.2)
        if not readings:
            self.mode = "none"
            self.degraded_reason = None
            return
        self.baseline_ms = round(sum(readings) / len(readings), 1)
        self.samples.append({"phase": "baseline", "ms": self.baseline_ms,
                             "mode": self.mode, "ok": True})

    def check(self, phase: str) -> None:
        """Sample once and raise :class:`ProbeAborted` if the instrument is degrading."""
        if self.mode in ("none", "disabled"):
            return
        value, error = self._sample()
        entry: Dict[str, Any] = {"phase": phase, "ms": value, "mode": self.mode,
                                 "ok": value is not None, "error": error}
        self.samples.append(entry)
        if value is None:
            self._consecutive_bad += 1
            entry["verdict"] = f"no answer ({error})"
        elif self.baseline_ms is not None and value > max(self.baseline_ms * 4.0,
                                                          self.baseline_ms + 500.0):
            self._consecutive_bad += 1
            entry["verdict"] = (f"{value:.0f} ms against a {self.baseline_ms:.0f} ms "
                                "baseline")
        else:
            self._consecutive_bad = 0
            entry["verdict"] = "ok"
        if self._consecutive_bad >= 2:
            self.degraded_reason = (
                f"health canary degraded twice in a row after phase {phase!r}: "
                f"{entry['verdict']}"
            )
            raise ProbeAborted(self.degraded_reason)


# ==========================================================================
# The link: one session, with the safety rules attached
# ==========================================================================


def _mnemonic_nodes(head: str) -> List[str]:
    return [node for node in head.upper().lstrip(":").split(":") if node]


class Link:
    """One instrument session, rate limited, refusing forbidden commands,
    and rebuilding itself after any hang.

    Every command that goes to the instrument goes through here, so
    ``--dry-run`` only has to be honoured in one place: in dry-run mode nothing
    is opened, every command is recorded, and queries return ``None`` -- which
    callers must treat as "unknown", exactly as they treat a hang.
    """

    def __init__(self, host: str, transport_kind: str, *, timeout: float,
                 never_send: Sequence[str] = (), dry_run: bool = False,
                 verbose: bool = False, ops_per_second: int = MAX_OPS_PER_SECOND):
        self.host = host
        self.transport_kind = transport_kind
        self.timeout = timeout
        self.dry_run = dry_run
        self.verbose = verbose
        self.transport: Optional[Transport] = None
        self.ops = 0
        self.timeouts = 0
        self.consecutive_timeouts = 0
        self.rebuilds = 0
        self.sent: List[Dict[str, Any]] = []
        self._buffer = bytearray()
        self._min_interval = 1.0 / max(ops_per_second, 1)
        self._last_op = 0.0
        self._forbidden = tuple(tuple(_mnemonic_nodes(entry.rstrip("?")))
                                for entry in never_send)
        self._forbidden_source = list(never_send)

    # -- transport lifecycle ---------------------------------------------

    def _build(self) -> Transport:
        if self.transport_kind == "raw":
            return RawSocketTransport(self.host)
        if self.transport_kind == "vxi11":
            return Vxi11Transport(self.host)
        raise ProbeLinkError(f"unknown transport {self.transport_kind!r}")

    def connect(self) -> None:
        if self.dry_run:
            return
        transport = self._build()
        transport.open(CONNECT_TIMEOUT, self.timeout)
        self.transport = transport
        self._buffer.clear()

    def close(self, graceful: bool = True) -> None:
        transport, self.transport = self.transport, None
        if transport is not None:
            transport.close(graceful)
        self._buffer.clear()

    def rebuild(self, why: str) -> bool:
        """Tear the link down and build a new one, with backoff.  Never a tight loop."""
        if self.dry_run:
            return True
        self.close(graceful=False)
        self.rebuilds += 1
        for wait in RECONNECT_BACKOFF:
            time.sleep(wait)
            try:
                self.connect()
            except (ProbeLinkError, OSError) as exc:
                last = exc
                continue
            if self.verbose:
                print(f"    link rebuilt after {why} (attempt cost {wait:g} s)",
                      flush=True)
            return True
        raise ProbeLinkError(f"could not rebuild the link after {why}: {last}")

    @property
    def describe(self) -> str:
        return self.transport.describe() if self.transport else f"{self.transport_kind} (not open)"

    # -- guards ----------------------------------------------------------

    def _check_allowed(self, command: str) -> None:
        if not self._forbidden:
            return
        head = command.split(" ", 1)[0].split(";", 1)[0]
        nodes = _mnemonic_nodes(head.rstrip("?"))
        for forbidden in self._forbidden:
            if len(nodes) != len(forbidden):
                continue
            if all(node.startswith(want[:4]) or want.startswith(node[:4])
                   for node, want in zip(nodes, forbidden)):
                raise ProbeForbidden(
                    f"{command!r} matches the profile's never-send list "
                    f"({':'.join(forbidden)})"
                )

    def _pace(self) -> None:
        gap = self._min_interval - (time.monotonic() - self._last_op)
        if gap > 0:
            time.sleep(gap)
        self._last_op = time.monotonic()

    def _record(self, command: str, timeout: float) -> None:
        self.sent.append({"command": command, "timeout": timeout})
        if self.verbose or self.dry_run:
            marker = "would send" if self.dry_run else "->"
            print(f"    {marker} {command}   [{timeout:g} s]", flush=True)

    # -- reading ---------------------------------------------------------

    def _fill(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportTimeout()
        assert self.transport is not None
        self._buffer += self.transport.recv(min(remaining, 1.0))

    def _read_line(self, command: str, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[:index])
                del self._buffer[: index + 1]
                return line.rstrip(b"\r")
            try:
                self._fill(deadline)
            except TransportTimeout:
                if time.monotonic() >= deadline:
                    raise ProbeTimeout(command, timeout) from None

    def _read_block(self, command: str, timeout: float) -> bytes:
        """Read an IEEE-488.2 block, definite or indefinite length.

        Keysight answers ``#<digits><length><bytes>``.  Keithley's binary
        formats answer ``#0`` followed by the payload and a newline (the
        DMM6500 reference manual is explicit about this), so both shapes have
        to be understood before either instrument's data can be read at all.
        """
        deadline = time.monotonic() + timeout
        while not self._buffer:
            try:
                self._fill(deadline)
            except TransportTimeout:
                if time.monotonic() >= deadline:
                    raise ProbeTimeout(command, timeout) from None
        if self._buffer[0:1] != b"#":
            return self._read_line(command, timeout)
        while len(self._buffer) < 2:
            try:
                self._fill(deadline)
            except TransportTimeout:
                if time.monotonic() >= deadline:
                    raise ProbeTimeout(command, timeout) from None
        digits = self._buffer[1] - 0x30
        if digits == 0:
            del self._buffer[:2]
            return self._read_line(command, timeout)
        while len(self._buffer) < 2 + digits:
            try:
                self._fill(deadline)
            except TransportTimeout:
                if time.monotonic() >= deadline:
                    raise ProbeTimeout(command, timeout) from None
        try:
            length = int(bytes(self._buffer[2: 2 + digits]))
        except ValueError:
            raise ProbeLinkError(
                f"{command!r} answered with a malformed block header"
            ) from None
        want = 2 + digits + length
        while len(self._buffer) < want:
            try:
                self._fill(deadline)
            except TransportTimeout:
                if time.monotonic() >= deadline:
                    raise ProbeTimeout(command, timeout) from None
        body = bytes(self._buffer[2 + digits: want])
        del self._buffer[:want]
        if self._buffer[:1] == b"\n":
            del self._buffer[:1]
        return body

    # -- the public surface ----------------------------------------------

    def write(self, command: str) -> None:
        self._check_allowed(command)
        self._record(command, 0.0)
        if self.dry_run:
            return
        self._pace()
        self.ops += 1
        if self.transport is None:
            raise ProbeLinkError(f"the link is not open; cannot send {command!r}")
        self.transport.send(command.encode("ascii") + b"\n")

    def query(self, command: str, timeout: Optional[float] = None) -> Optional[str]:
        """Send a query and return its reply, or ``None`` if it hung.

        A hang is not an error the caller has to handle: the link is rebuilt
        here, the timeout is counted, and ``None`` comes back.  That is what
        makes a sweep of unknown candidates survivable -- one bad command does
        not invalidate the rest of the run.
        """
        raw = self.query_bytes(command, timeout)
        return None if raw is None else raw.decode("latin-1", "replace").strip()

    def query_bytes(self, command: str, timeout: Optional[float] = None,
                    block: bool = False) -> Optional[bytes]:
        self._check_allowed(command)
        limit = self.timeout if timeout is None else timeout
        self._record(command, limit)
        if self.dry_run:
            return None
        self._pace()
        self.ops += 1
        if self.transport is None:
            raise ProbeLinkError(f"the link is not open; cannot send {command!r}")
        self._buffer.clear()
        self.transport.send(command.encode("ascii") + b"\n")
        try:
            if block:
                data = self._read_block(command, limit)
            else:
                data = self._read_line(command, limit)
        except ProbeTimeout:
            self.timeouts += 1
            self.consecutive_timeouts += 1
            if self.timeouts > MAX_TOTAL_TIMEOUTS:
                raise ProbeAborted(
                    f"{self.timeouts} commands have hung; stopping rather than "
                    "continuing to poke an instrument that is not answering"
                ) from None
            if self.consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                raise ProbeAborted(
                    f"{self.consecutive_timeouts} commands hung in a row; the link "
                    "is gone, not the commands"
                ) from None
            self.rebuild(f"{command!r} timed out")
            return None
        except ProbeLinkError:
            self.rebuild(f"{command!r} broke the link")
            return None
        self.consecutive_timeouts = 0
        return data


# ==========================================================================
# Profiles -- the candidate command lists
# ==========================================================================
#
# A profile is data, never behaviour.  Everything the probe sends comes from
# one of these (or from a --candidates JSON file with the same shape), which is
# what makes --dry-run an honest account of the run.
#
# Only the keysight-34461A profile contains commands that have touched
# hardware.  The Keithley profiles are transcribed from the manufacturer's
# reference manuals and are candidates *to be tested*, not facts.

PROFILE_SCHEMA_NOTE = """\
A candidates file is JSON with these keys (all optional except `name`):

  name          str        identifier for the report
  description   str
  source        str        where the commands came from; printed in the report
  verified      bool       false unless every command has touched hardware
  match         [str]      regexes tried against *IDN? for --profile auto
  never_send    [str]      the link refuses these outright
  core          {}         idn/error/clear/abort/language/state/local commands
  queries       [str]      candidate queries to sweep
  writes        [{send, readback}]  candidate writes, swept only with --sweep-writes
  functions     [{}]       per function: key/label/select/readback/range_*/nplc_*
  throughput    {}         setup/points/start/count/fetch/stop
  screen        {scpi: [{label, setup, query}], http: [str]}
  crash         {arm: [str], observe: [str], stop: [str]}
"""


KEYSIGHT_34461A: Dict[str, Any] = {
    "name": "keysight-34461a",
    "description": "Keysight 34461A Truevolt. The reference profile.",
    "source": "SPEC.md sections 1-2, established against the physical "
              "instrument at 192.0.2.50 on 2026-08-27.",
    "verified": True,
    "match": [r"34461A", r"3446\dA"],
    "never_send": [
        "SAMP:SOUR?", "SAMP:TIM?", "SAMP:COUN:PRET?", "TRIG:LEV?",
        "RES:OCOM?", "FRES:OCOM?", "CONT:THR?", "DIOD:THR?", "TEMP:UNIT?",
        "VOLT:DC:APER:ENAB?", "CALC:SCAL:REF?", "CALC:SCAL:GAIN?",
        "CALC:SCAL:OFFS?", "CALC:SCAL:PCT?", "CALC:SCAL:UNIT?",
        "CALC:SCAL:UNIT:STAT?", "DISP:ANN:STAT?", "DISP:DIG:MASK?",
        "SYST:PRES?", "SYST:LANG?", "SYST:IDN?", "SYST:HELP:HEAD?",
        "CAP:RES?", "FREQ:RES?", "PER:RES?", "TEMP:RES?",
        "FRES:ZERO:AUTO?", "VOLT:AC:ZERO:AUTO?",
    ],
    "core": {
        "idn": "*IDN?",
        "clear": "*CLS",
        "error": "SYST:ERR?",
        "abort": "ABOR",
        "language": None,
        "state": "STAT:OPER:COND?",
        "local": ["SYST:LOC"],
        "reading_count": "DATA:POIN?",
    },
    "queries": [
        "*IDN?", "*OPT?", "*ESR?", "*STB?",
        "SYST:ERR?", "SYST:UPT?", "SYST:LFR?", "SYST:DATE?", "SYST:TIME?",
        "SYST:BEEP:STAT?", "SYST:CLIC:STAT?", "SYST:LOCK:OWN?",
        "SYST:SEC:COUN?", "ROUT:TERM?",
        "STAT:QUES:COND?", "STAT:OPER:COND?",
        "SYST:COMM:LAN:HOST?", "SYST:COMM:LAN:IPAD?", "SYST:COMM:LAN:MAC?",
        "SYST:COMM:LAN:DHCP?", "LXI:IDEN:STAT?",
        "CAL:DATE?", "CAL:COUN?", "CAL:STR?",
        "CONF?", "SENS:FUNC?",
        "TRIG:SOUR?", "TRIG:DEL?", "TRIG:DEL:AUTO?", "TRIG:COUN?", "TRIG:SLOP?",
        "SAMP:COUN?", "DATA:POIN?", "DATA:LAST?",
        "CALC:AVER:STAT?", "CALC:AVER:COUN?", "CALC:SCAL:STAT?",
        "CALC:LIM:STAT?", "CALC:TRAN:HIST:STAT?", "CALC:TRAN:HIST:POIN?",
        "DISP?", "DISP:VIEW?", "DISP:TEXT?",
        "VOLT:DC:NPLC?", "VOLT:DC:APER?", "VOLT:DC:IMP:AUTO?",
        "VOLT:DC:ZERO:AUTO?", "VOLT:DC:RES?", "UNIT:TEMP?",
    ],
    "writes": [],
    "functions": [
        {"key": "VOLT:DC", "label": "DC Voltage", "short": "DCV", "unit": "V",
         "select": "CONF:VOLT:DC", "readback": "SENS:FUNC?",
         "range_query": "VOLT:DC:RANG?", "range_set": "VOLT:DC:RANG {value}",
         "range_auto": "VOLT:DC:RANG:AUTO {state}",
         "nplc_query": "VOLT:DC:NPLC?", "resolution_query": "VOLT:DC:RES?"},
        {"key": "VOLT:AC", "label": "AC Voltage", "short": "ACV", "unit": "V",
         "select": "CONF:VOLT:AC", "readback": "SENS:FUNC?",
         "range_query": "VOLT:AC:RANG?", "range_set": "VOLT:AC:RANG {value}",
         "range_auto": "VOLT:AC:RANG:AUTO {state}",
         "resolution_query": "VOLT:AC:RES?"},
        {"key": "CURR:DC", "label": "DC Current", "short": "DCI", "unit": "A",
         "select": "CONF:CURR:DC", "readback": "SENS:FUNC?",
         "range_query": "CURR:DC:RANG?", "range_set": "CURR:DC:RANG {value}",
         "range_auto": "CURR:DC:RANG:AUTO {state}",
         "nplc_query": "CURR:DC:NPLC?", "resolution_query": "CURR:DC:RES?"},
        {"key": "RES", "label": "2-Wire Resistance", "short": "2W", "unit": "ohm",
         "select": "CONF:RES", "readback": "SENS:FUNC?",
         "range_query": "RES:RANG?", "range_set": "RES:RANG {value}",
         "range_auto": "RES:RANG:AUTO {state}",
         "nplc_query": "RES:NPLC?", "resolution_query": "RES:RES?"},
        {"key": "CAP", "label": "Capacitance", "short": "CAP", "unit": "F",
         "select": "CONF:CAP", "readback": "SENS:FUNC?",
         "range_query": "CAP:RANG?", "range_set": "CAP:RANG {value}",
         "range_auto": "CAP:RANG:AUTO {state}"},
    ],
    "throughput": {
        "setup": ["ABOR", "*CLS", "CONF:VOLT:DC AUTO",
                  "TRIG:SOUR IMM", "SAMP:COUN 1", "TRIG:COUN INF"],
        "points": [
            {"label": "NPLC 1", "commands": ["VOLT:DC:NPLC 1"]},
            {"label": "NPLC 0.2", "commands": ["VOLT:DC:NPLC 0.2"]},
            {"label": "NPLC 0.02", "commands": ["VOLT:DC:NPLC 0.02"]},
        ],
        "start": "INIT",
        "count_query": "DATA:POIN?",
        "fetch": "R? {count}",
        "fetch_is_block": True,
        "stop": ["ABOR"],
    },
    "screen": {
        "scpi": [
            {"label": "BMP", "setup": ["HCOP:SDUM:DATA:FORM BMP"],
             "query": "HCOP:SDUM:DATA?", "timeout": 15.0},
            {"label": "PNG", "setup": ["HCOP:SDUM:DATA:FORM PNG"],
             "query": "HCOP:SDUM:DATA?", "timeout": 15.0},
        ],
        "http": [],
    },
    "crash": {
        "arm": ["ABOR", "*CLS", "CONF:VOLT:DC AUTO", "TRIG:SOUR IMM",
                "SAMP:COUN 1", "TRIG:COUN INF", "INIT"],
        "observe": ["DATA:POIN?", "STAT:OPER:COND?"],
        "stop": ["ABOR", "*CLS"],
    },
    "restore": {
        "capture": ["SENS:FUNC?", "TRIG:SOUR?", "TRIG:COUN?", "SAMP:COUN?",
                    "TRIG:DEL:AUTO?"],
        "apply": ["ABOR", "TRIG:SOUR {TRIG:SOUR?}", "TRIG:COUN {TRIG:COUN?}",
                  "SAMP:COUN {SAMP:COUN?}"],
    },
}


# --------------------------------------------------------------------------
# Keithley DMM6500 / DMM7510.
#
# EVERY command below is UNVERIFIED.  It is transcribed from the manufacturer's
# reference manuals (DMM6500-901-01 Rev. A, April 2018; DMM7510-901-01 Rev. C,
# September 2019) and has not been sent to hardware by anyone on this project.
# See docs/keithley-dmm6500.md and docs/keithley-dmm7510.md for the citation of
# each one, and for what is known to be missing.
#
# `never_send` is EMPTY for both, and that is not an oversight: the 34461A's
# never-send list is a property of that instrument's firmware and does not
# transfer.  The point of running this probe is to discover what belongs here.
# --------------------------------------------------------------------------

_KEITHLEY_COMMON_QUERIES = [
    "*IDN?", "*LANG?", "*ESR?", "*STB?",
    ":SYSTem:ERRor:COUNt?", ":SYSTem:ERRor?", ":SYSTem:VERSion?",
    ":SYSTem:LFRequency?", ":SYSTem:TIME? 1", ":SYSTem:ACCess?",
    ":SYSTem:COMMunication:LAN:MACaddress?",
    ":SYSTem:EVENtlog:COUNt? ALL",
    ":STATus:OPERation:CONDition?", ":STATus:QUEStionable:CONDition?",
    ":ROUTe:TERMinals?",
    ":FORMat:DATA?", ":FORMat:BORDer?", ":FORMat:ASCii:PRECision?",
    ":SENSe:FUNCtion?", ":SENSe:DIGitize:FUNCtion?", ":SENSe:COUNt?",
    ":SENSe:DIGitize:COUNt?",
    ":TRACe:ACTual?", ":TRACe:ACTual:STARt?", ":TRACe:ACTual:END?",
    ":TRACe:POINts?", ":TRACe:FILL:MODE?", ":TRACe:LOG:STATe?",
    ":TRACe:STATistics:AVERage?", ":TRACe:STATistics:MINimum?",
    ":TRACe:STATistics:MAXimum?", ":TRACe:STATistics:PK2Pk?",
    ":TRACe:STATistics:STDDev?",
    ":TRIGger:STATe?", ":TRIGger:BLOCk:LIST?",
    ":DISPlay:BUFFer:ACTive?", ":DISPlay:LIGHt:STATe?",
    ":DISPlay:READing:FORMat?", ":DISPlay:VOLTage:DIGits?",
    ":CALCulate:VOLTage:MATH:STATe?", ":CALCulate:VOLTage:MATH:FORMat?",
    ":CALCulate2:VOLTage:LIMit1:STATe?",
    ":CALCulate2:VOLTage:LIMit1:LOWer?", ":CALCulate2:VOLTage:LIMit1:UPPer?",
    ":CALCulate2:VOLTage:LIMit1:FAIL?",
    ":SENSe:VOLTage:NPLCycles?", ":SENSe:VOLTage:NPLCycles? MIN",
    ":SENSe:VOLTage:NPLCycles? MAX", ":SENSe:VOLTage:APERture?",
    ":SENSe:VOLTage:RANGe?", ":SENSe:VOLTage:RANGe? MIN",
    ":SENSe:VOLTage:RANGe? MAX", ":SENSe:VOLTage:RANGe:AUTO?",
    ":SENSe:VOLTage:AZERo:STATe?", ":SENSe:VOLTage:INPutimpedance?",
    ":SENSe:VOLTage:LINE:SYNC?", ":SENSe:VOLTage:AVERage:STATe?",
    ":SENSe:VOLTage:AVERage:COUNt?", ":SENSe:VOLTage:AVERage:TCONtrol?",
    ":SENSe:VOLTage:RELative?", ":SENSe:VOLTage:RELative:STATe?",
    ":SENSe:VOLTage:DELay:AUTO?", ":SENSe:VOLTage:UNIT?",
    ":SENSe:VOLTage:DB:REFerence?", ":SENSe:VOLTage:DBM:REFerence?",
    ":SENSe:CONFiguration:LIST:CATalog?",
]

_KEITHLEY_FUNCTIONS = [
    {"key": "VOLT:DC", "label": "DC Voltage", "short": "DCV", "unit": "V",
     "select": ':SENSe:FUNCtion "VOLTage:DC"', "readback": ":SENSe:FUNCtion?",
     "range_query": ":SENSe:VOLTage:RANGe?",
     "range_min": ":SENSe:VOLTage:RANGe? MIN",
     "range_max": ":SENSe:VOLTage:RANGe? MAX",
     "range_set": ":SENSe:VOLTage:RANGe {value}",
     "range_auto": ":SENSe:VOLTage:RANGe:AUTO {state}",
     "nplc_query": ":SENSe:VOLTage:NPLCycles?",
     "nplc_min": ":SENSe:VOLTage:NPLCycles? MIN",
     "nplc_max": ":SENSe:VOLTage:NPLCycles? MAX",
     "aperture_query": ":SENSe:VOLTage:APERture?",
     "digits_query": ":DISPlay:VOLTage:DIGits?"},
    {"key": "VOLT:AC", "label": "AC Voltage", "short": "ACV", "unit": "V",
     "select": ':SENSe:FUNCtion "VOLTage:AC"', "readback": ":SENSe:FUNCtion?",
     "range_query": ":SENSe:VOLTage:AC:RANGe?",
     "range_min": ":SENSe:VOLTage:AC:RANGe? MIN",
     "range_max": ":SENSe:VOLTage:AC:RANGe? MAX",
     "range_set": ":SENSe:VOLTage:AC:RANGe {value}",
     "range_auto": ":SENSe:VOLTage:AC:RANGe:AUTO {state}",
     "aperture_query": ":SENSe:VOLTage:AC:APERture?",
     "extra": [":SENSe:VOLTage:AC:DETector:BANDwidth?"]},
    {"key": "CURR:DC", "label": "DC Current", "short": "DCI", "unit": "A",
     "select": ':SENSe:FUNCtion "CURRent:DC"', "readback": ":SENSe:FUNCtion?",
     "range_query": ":SENSe:CURRent:RANGe?",
     "range_min": ":SENSe:CURRent:RANGe? MIN",
     "range_max": ":SENSe:CURRent:RANGe? MAX",
     "range_set": ":SENSe:CURRent:RANGe {value}",
     "range_auto": ":SENSe:CURRent:RANGe:AUTO {state}",
     "nplc_query": ":SENSe:CURRent:NPLCycles?",
     "nplc_min": ":SENSe:CURRent:NPLCycles? MIN",
     "nplc_max": ":SENSe:CURRent:NPLCycles? MAX"},
    {"key": "CURR:AC", "label": "AC Current", "short": "ACI", "unit": "A",
     "select": ':SENSe:FUNCtion "CURRent:AC"', "readback": ":SENSe:FUNCtion?",
     "range_query": ":SENSe:CURRent:AC:RANGe?",
     "range_min": ":SENSe:CURRent:AC:RANGe? MIN",
     "range_max": ":SENSe:CURRent:AC:RANGe? MAX",
     "range_set": ":SENSe:CURRent:AC:RANGe {value}",
     "range_auto": ":SENSe:CURRent:AC:RANGe:AUTO {state}"},
    {"key": "RES", "label": "2-Wire Resistance", "short": "2W", "unit": "ohm",
     "select": ':SENSe:FUNCtion "RESistance"', "readback": ":SENSe:FUNCtion?",
     "range_query": ":SENSe:RESistance:RANGe?",
     "range_min": ":SENSe:RESistance:RANGe? MIN",
     "range_max": ":SENSe:RESistance:RANGe? MAX",
     "range_set": ":SENSe:RESistance:RANGe {value}",
     "range_auto": ":SENSe:RESistance:RANGe:AUTO {state}",
     "nplc_query": ":SENSe:RESistance:NPLCycles?",
     "extra": [":SENSe:RESistance:OCOMpensated?"]},
    {"key": "FRES", "label": "4-Wire Resistance", "short": "4W", "unit": "ohm",
     "select": ':SENSe:FUNCtion "FRESistance"', "readback": ":SENSe:FUNCtion?",
     "range_query": ":SENSe:FRESistance:RANGe?",
     "range_min": ":SENSe:FRESistance:RANGe? MIN",
     "range_max": ":SENSe:FRESistance:RANGe? MAX",
     "range_set": ":SENSe:FRESistance:RANGe {value}",
     "range_auto": ":SENSe:FRESistance:RANGe:AUTO {state}",
     "nplc_query": ":SENSe:FRESistance:NPLCycles?",
     "extra": [":SENSe:FRESistance:OCOMpensated?"]},
    {"key": "FREQ", "label": "Frequency", "short": "FREQ", "unit": "Hz",
     "select": ':SENSe:FUNCtion "FREQuency:VOLTage"',
     "readback": ":SENSe:FUNCtion?",
     "range_query": ":SENSe:FREQuency:THReshold:RANGe?",
     "range_min": ":SENSe:FREQuency:THReshold:RANGe? MIN",
     "range_max": ":SENSe:FREQuency:THReshold:RANGe? MAX",
     "range_set": ":SENSe:FREQuency:THReshold:RANGe {value}",
     "range_auto": ":SENSe:FREQuency:THReshold:RANGe:AUTO {state}",
     "aperture_query": ":SENSe:FREQuency:APERture?"},
    {"key": "PER", "label": "Period", "short": "PER", "unit": "s",
     "select": ':SENSe:FUNCtion "PERiod:VOLTage"',
     "readback": ":SENSe:FUNCtion?",
     "range_query": ":SENSe:PERiod:THReshold:RANGe?",
     "range_min": ":SENSe:PERiod:THReshold:RANGe? MIN",
     "range_max": ":SENSe:PERiod:THReshold:RANGe? MAX",
     "aperture_query": ":SENSe:PERiod:APERture?"},
    {"key": "CAP", "label": "Capacitance", "short": "CAP", "unit": "F",
     "select": ':SENSe:FUNCtion "CAPacitance"', "readback": ":SENSe:FUNCtion?",
     "range_query": ":SENSe:CAPacitance:RANGe?",
     "range_min": ":SENSe:CAPacitance:RANGe? MIN",
     "range_max": ":SENSe:CAPacitance:RANGe? MAX",
     "range_set": ":SENSe:CAPacitance:RANGe {value}",
     "range_auto": ":SENSe:CAPacitance:RANGe:AUTO {state}"},
    {"key": "CONT", "label": "Continuity", "short": "CONT", "unit": "ohm",
     "select": ':SENSe:FUNCtion "CONTinuity"', "readback": ":SENSe:FUNCtion?"},
    {"key": "DIOD", "label": "Diode", "short": "DIODE", "unit": "V",
     "select": ':SENSe:FUNCtion "DIODe"', "readback": ":SENSe:FUNCtion?",
     "extra": [":SENSe:DIODe:BIAS:LEVel?"]},
    {"key": "TEMP", "label": "Temperature", "short": "TEMP", "unit": "deg",
     "select": ':SENSe:FUNCtion "TEMPerature"', "readback": ":SENSe:FUNCtion?",
     "nplc_query": ":SENSe:TEMPerature:NPLCycles?",
     "extra": [":SENSe:TEMPerature:TRANsducer?", ":SENSe:TEMPerature:UNIT?",
               ":SENSe:TEMPerature:TCouple:TYPE?",
               ":SENSe:TEMPerature:THERmistor?"]},
    {"key": "VOLT:DC:RAT", "label": "DC Voltage Ratio", "short": "RATIO",
     "unit": "", "select": ':SENSe:FUNCtion "VOLTage:DC:RATio"',
     "readback": ":SENSe:FUNCtion?",
     "range_query": ":SENSe:VOLTage:RATio:RANGe?"},
    {"key": "DIG:VOLT", "label": "Digitize Voltage", "short": "DIGV", "unit": "V",
     "select": ':SENSe:DIGitize:FUNCtion "VOLTage"',
     "readback": ":SENSe:DIGitize:FUNCtion?",
     "range_query": ":SENSe:DIGitize:VOLTage:RANGe?",
     "range_min": ":SENSe:DIGitize:VOLTage:RANGe? MIN",
     "range_max": ":SENSe:DIGitize:VOLTage:RANGe? MAX",
     "extra": [":SENSe:DIGitize:VOLTage:SRATe?",
               ":SENSe:DIGitize:VOLTage:SRATe? MAX",
               ":SENSe:DIGitize:VOLTage:APERture?"]},
    {"key": "DIG:CURR", "label": "Digitize Current", "short": "DIGI", "unit": "A",
     "select": ':SENSe:DIGitize:FUNCtion "CURRent"',
     "readback": ":SENSe:DIGitize:FUNCtion?",
     "range_query": ":SENSe:DIGitize:CURRent:RANGe?",
     "range_min": ":SENSe:DIGitize:CURRent:RANGe? MIN",
     "range_max": ":SENSe:DIGitize:CURRent:RANGe? MAX",
     "extra": [":SENSe:DIGitize:CURRent:SRATe?"]},
]

_KEITHLEY_THROUGHPUT = {
    "setup": [":ABORt", "*CLS", ':SENSe:FUNCtion "VOLTage:DC"',
              ":SENSe:VOLTage:RANGe:AUTO ON", ':TRACe:CLEar "defbuffer1"'],
    "points": [
        {"label": "NPLC 1", "commands": [":SENSe:VOLTage:NPLCycles 1"]},
        {"label": "NPLC 0.2", "commands": [":SENSe:VOLTage:NPLCycles 0.2"]},
        {"label": "NPLC 0.0005 (fastest)",
         "commands": [":SENSe:VOLTage:NPLCycles 0.0005"]},
    ],
    # SimpleLoop with a large finite count is the closest documented analogue of
    # TRIG:COUN INF.  It is finite on purpose: an unbounded acquisition on an
    # uncharacterised instrument is exactly what IO-DISCIPLINE.md rule 1 warns
    # against, and defbuffer1 holds 100,000 readings by default.
    "start": ':TRIGger:LOAD "SimpleLoop", 20000, 0, "defbuffer1"',
    "start_extra": [":INITiate"],
    "count_query": ':TRACe:ACTual? "defbuffer1"',
    "fetch": ':TRACe:DATA? {start}, {end}, "defbuffer1", READing',
    "fetch_is_block": False,
    "fetch_indexed": True,
    "stop": [":ABORt", ':TRACe:CLEar "defbuffer1"'],
}

_KEITHLEY_CRASH = {
    "arm": [":ABORt", "*CLS", ':SENSe:FUNCtion "VOLTage:DC"',
            ":SENSe:VOLTage:NPLCycles 1",
            ':TRACe:CLEar "defbuffer1"',
            ':TRIGger:LOAD "SimpleLoop", 100000, 0, "defbuffer1"',
            ":INITiate"],
    "observe": [":TRIGger:STATe?", ':TRACe:ACTual? "defbuffer1"'],
    "stop": [":ABORt", "*CLS", ':TRACe:CLEar "defbuffer1"'],
}

#: Guesses.  The virtual front panel serves the real screen over HTTP at
#: 800x480 (or 400x240) and its context menu offers "Download screenshot", but
#: neither reference manual names the URL.  These are plausible paths for the
#: probe to try with a plain GET; none is documented and none has been tested.
_KEITHLEY_HTTP_SCREEN = [
    "/screenshot.png", "/screen.png", "/screen", "/getScreen",
    "/screenshot", "/vfp/screen.png", "/lxi/screenshot",
]

KEITHLEY_DMM6500: Dict[str, Any] = {
    "name": "keithley-dmm6500",
    "description": "Keithley DMM6500 6.5-digit bench/system DMM, SCPI language.",
    "source": "DMM6500 Reference Manual DMM6500-901-01 Rev. A / April 2018, "
              "section 13. NOT VERIFIED AGAINST HARDWARE.",
    "verified": False,
    "match": [r"DMM6500"],
    "never_send": [],
    "core": {
        "idn": "*IDN?",
        "clear": "*CLS",
        "error": ":SYSTem:ERRor?",
        "abort": ":ABORt",
        "language": "*LANG?",
        "state": ":TRIGger:STATe?",
        # UNVERIFIED. `TRIGger:CONTinuous` is documented for the DMM7510 and was
        # added to the DMM6500 in firmware 1.7.0 ("Added remote commands to set
        # continuous measurement"); a user report on eevblog.com describes
        # `TRIG:CONT REST` followed by `logout` working on a DMM6500.  Both are
        # probed, and either failing is recorded rather than assumed away.
        "local": [":TRIGger:CONTinuous RESTart", "logout"],
        "reading_count": ':TRACe:ACTual? "defbuffer1"',
    },
    "queries": _KEITHLEY_COMMON_QUERIES + [
        ":SYSTem:CARD1:IDN?", ":ROUTe:SCAN:STATe?",
        ":TRACe:STATistics:SPAN?",
    ],
    "writes": [],
    "functions": _KEITHLEY_FUNCTIONS,
    "throughput": _KEITHLEY_THROUGHPUT,
    "screen": {
        # Neither Keithley manual documents any SCPI screen-capture command.
        # The probe still tries the two shapes other vendors use, precisely so
        # the report can say "asked, and it did not answer" rather than
        # "assumed absent".
        "scpi": [
            {"label": "HCOPy (Keysight shape)",
             "setup": [], "query": ":HCOPy:SDUMp:DATA?", "timeout": 10.0},
            {"label": "DISPlay:DATA (Rigol/Siglent shape)",
             "setup": [], "query": ":DISPlay:DATA?", "timeout": 10.0},
        ],
        "http": _KEITHLEY_HTTP_SCREEN,
    },
    "crash": _KEITHLEY_CRASH,
    "restore": {
        "capture": [":SENSe:FUNCtion?", ":SENSe:VOLTage:RANGe:AUTO?",
                    ":SENSe:VOLTage:NPLCycles?", ":SENSe:COUNt?",
                    ":TRIGger:STATe?"],
        "apply": [":ABORt", ':SENSe:FUNCtion {SENSe:FUNCtion?}',
                  ":SENSe:COUNt {SENSe:COUNt?}"],
    },
}

KEITHLEY_DMM7510: Dict[str, Any] = {
    "name": "keithley-dmm7510",
    "description": "Keithley DMM7510 7.5-digit graphical sampling DMM, SCPI language.",
    "source": "DMM7510 Reference Manual DMM7510-901-01 Rev. C / September 2019, "
              "section 11. NOT VERIFIED AGAINST HARDWARE.",
    "verified": False,
    "match": [r"DMM7510"],
    "never_send": [],
    "core": {
        "idn": "*IDN?",
        "clear": "*CLS",
        "error": ":SYSTem:ERRor?",
        "abort": ":ABORt",
        "language": "*LANG?",
        "state": ":TRIGger:STATe?",
        # Documented for the DMM7510 at DMM7510-901-01 Rev. C page 11-191.
        # Still UNVERIFIED here: nobody on this project has sent it.
        "local": [":TRIGger:CONTinuous RESTart", "logout"],
        "reading_count": ':TRACe:ACTual? "defbuffer1"',
    },
    "queries": _KEITHLEY_COMMON_QUERIES + [
        ":ACAL:COUNt?", ":ACAL:LASTrun:TIME?", ":ACAL:NEXTrun:TIME?",
        ":ACAL:LASTrun:TEMPerature:INTernal?",
        ":ACAL:LASTrun:TEMPerature:DIFFerence?", ":ACAL:SCHedule?",
        ":SYSTem:TEMPerature:INTernal?", ":SYSTem:FAN:LEVel?",
        ":TRIGger:CONTinuous?",
        ":SENSe:VOLTage:AC:FREQuency?",
        ":SENSe:DIGitize:VOLTage:COUPling?",
        ":SENSe:DIGitize:VOLTage:DCIRcuit?",
    ],
    "writes": [],
    "functions": _KEITHLEY_FUNCTIONS,
    "throughput": _KEITHLEY_THROUGHPUT,
    "screen": {
        "scpi": [
            {"label": "HCOPy (Keysight shape)",
             "setup": [], "query": ":HCOPy:SDUMp:DATA?", "timeout": 10.0},
            {"label": "DISPlay:DATA (Rigol/Siglent shape)",
             "setup": [], "query": ":DISPlay:DATA?", "timeout": 10.0},
        ],
        "http": _KEITHLEY_HTTP_SCREEN,
    },
    "crash": _KEITHLEY_CRASH,
    "restore": {
        "capture": [":SENSe:FUNCtion?", ":SENSe:VOLTage:RANGe:AUTO?",
                    ":SENSe:VOLTage:NPLCycles?", ":SENSe:COUNt?",
                    ":TRIGger:STATe?"],
        "apply": [":ABORt", ':SENSe:FUNCtion {SENSe:FUNCtion?}',
                  ":SENSe:COUNt {SENSe:COUNt?}"],
    },
}

GENERIC: Dict[str, Any] = {
    "name": "generic",
    "description": "IEEE 488.2 common commands only. Safe against anything "
                   "that speaks SCPI at all, and learns nothing model specific.",
    "source": "IEEE 488.2 mandated common commands.",
    "verified": False,
    "match": [],
    "never_send": [],
    "core": {"idn": "*IDN?", "clear": "*CLS", "error": "SYST:ERR?",
             "abort": None, "language": "*LANG?", "state": None,
             "local": [], "reading_count": None},
    "queries": ["*IDN?", "*ESR?", "*STB?", "*SRE?", "*ESE?", "*OPC?",
                "*LANG?", "*OPT?", "SYST:ERR?", "SYST:VERS?"],
    "writes": [],
    "functions": [],
    "throughput": {},
    "screen": {"scpi": [], "http": []},
    "crash": {},
    "restore": {"capture": [], "apply": []},
}

PROFILES: Dict[str, Dict[str, Any]] = {
    profile["name"]: profile
    for profile in (KEYSIGHT_34461A, KEITHLEY_DMM6500, KEITHLEY_DMM7510, GENERIC)
}


def select_profile(name: str, idn: Optional[str]) -> Dict[str, Any]:
    if name != "auto":
        if name not in PROFILES:
            raise SystemExit(f"unknown profile {name!r}; "
                             f"choose from {', '.join(sorted(PROFILES))} or auto")
        return PROFILES[name]
    if idn:
        for profile in PROFILES.values():
            for pattern in profile.get("match", ()):
                if re.search(pattern, idn, re.IGNORECASE):
                    return profile
    return GENERIC


def load_candidates(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if "name" not in data:
        raise SystemExit(f"{path}: a candidates file needs a \"name\"")
    merged = dict(GENERIC)
    merged.update(data)
    merged.setdefault("verified", False)
    return merged


# ==========================================================================
# The probe
# ==========================================================================


class Probe:
    """Runs the phases and accumulates the report.

    Every phase is bounded, every phase is followed by a canary check, and the
    whole thing is wrapped so teardown runs on any exit path.
    """

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.host = args.host
        self.started = time.time()
        self.deadline = time.monotonic() + args.max_seconds
        self.canary = Canary(self.host, enabled=not args.no_canary)
        self.profile: Dict[str, Any] = GENERIC
        self.link: Optional[Link] = None
        self.report: Dict[str, Any] = {
            "meta": {
                "tool": "tools/probe_instrument.py",
                "tool_version": TOOL_VERSION,
                "host": self.host,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started)),
                "argv": sys.argv[1:],
                "python": platform.python_version(),
                "platform": platform.platform(),
                "dry_run": args.dry_run,
            },
            "profile": {},
            "transports": {},
            "identity": {},
            "canary": {},
            "commands": [],
            "functions": [],
            "throughput": [],
            "screen": {"scpi": [], "http": []},
            "crash_safety": [],
            "teardown": [],
            "aborted": None,
            "warnings": [],
        }
        self.saved_state: Dict[str, str] = {}

    # -- small helpers ---------------------------------------------------

    def say(self, message: str) -> None:
        print(message, flush=True)

    def warn(self, message: str) -> None:
        self.report["warnings"].append(message)
        print(f"  WARNING: {message}", flush=True)

    def budget(self, what: str) -> None:
        if time.monotonic() > self.deadline:
            raise ProbeAborted(f"the {self.args.max_seconds:g} s run budget ran "
                               f"out during {what}")

    def gap(self, phase: str) -> None:
        self.budget(phase)
        if not self.args.dry_run:
            time.sleep(PHASE_GAP)
            self.canary.check(phase)

    def error_queue(self) -> Optional[str]:
        """Drain one error, if the profile knows how to ask.  One operation."""
        query = self.profile.get("core", {}).get("error")
        if not query or self.link is None:
            return None
        return self.link.query(query, timeout=min(self.args.timeout, 3.0))

    # -- phase 1: transports ---------------------------------------------

    def phase_transports(self) -> None:
        self.say("\n[1/8] transports")
        transports: Dict[str, Any] = {}
        if self.args.dry_run:
            transports["note"] = ("dry run: no sockets opened; the probe would "
                                  "TCP-connect to 5025, 23, 111, 1024, 4880, "
                                  "5030 and 80")
            self.report["transports"] = transports
            return
        ports = {
            "raw_scpi": RAW_SCPI_PORT, "telnet": TELNET_PORT,
            "portmapper": PORTMAP_PORT, "vxi11_direct": VXI11_DIRECT_PORT,
            "hislip": HISLIP_PORT, "dead_socket": DST_PORT, "http": HTTP_PORT,
        }
        for label, port in ports.items():
            result = tcp_probe(self.host, port)
            transports[label] = result
            state = "open" if result["open"] else "closed"
            detail = f" ({result['ms']} ms)" if result["open"] else ""
            self.say(f"  {label:14s} :{port:<5d} {state}{detail}")

        # VXI-11 proper: is there a core channel, and can a link be created?
        vxi = {"available": False, "core_port": None, "port_source": None,
               "detail": None}
        transport = Vxi11Transport(self.host)
        try:
            port = transport.lookup_core_port(CONNECT_TIMEOUT)
            vxi["core_port"] = port
            vxi["port_source"] = transport.port_source
            transport.open(CONNECT_TIMEOUT, self.args.timeout)
            vxi["available"] = True
            vxi["detail"] = "create_link on inst0 succeeded"
        except (ProbeLinkError, OSError, TransportTimeout) as exc:
            vxi["detail"] = str(exc)
        finally:
            transport.close()
        transports["vxi11"] = vxi
        self.say(f"  VXI-11         {'available' if vxi['available'] else 'no'}"
                 f" — {vxi['detail']}")

        hislip = hislip_probe(self.host)
        transports["hislip_detail"] = hislip
        self.say(f"  HiSLIP         {'available' if hislip['available'] else 'no'}"
                 f" — {hislip['detail']}")

        self.report["transports"] = transports

    # -- phase 2: identity and language ----------------------------------

    def phase_identity(self) -> None:
        self.say("\n[2/8] identity")
        assert self.link is not None
        identity: Dict[str, Any] = {}
        idn = self.link.query(self.profile["core"].get("idn") or "*IDN?")
        identity["idn"] = idn
        if idn:
            self.say(f"  *IDN? -> {idn}")
            fields = [part.strip() for part in idn.split(",")]
            identity["fields"] = fields
            if len(fields) >= 4:
                identity["manufacturer"], identity["model"] = fields[0], fields[1]
                identity["serial"], identity["firmware"] = fields[2], fields[3]
        elif not self.args.dry_run:
            self.warn("*IDN? did not answer; everything after this is guesswork")

        language_query = self.profile["core"].get("language")
        if language_query:
            language = self.link.query(language_query)
            identity["language_query"] = language_query
            identity["language"] = language
            if language:
                self.say(f"  {language_query} -> {language}")

        # An unambiguous dialect probe: in TSP the instrument evaluates Lua and
        # echoes the string; in SCPI it does not understand the line at all.
        # It is read-only either way and never switches anything.
        if self.args.tsp_probe:
            echo = self.link.query('print("PROBE_TSP_ECHO")', timeout=2.0)
            identity["tsp_echo"] = echo
            identity["tsp_mode"] = (echo is not None
                                    and "PROBE_TSP_ECHO" in echo)
            if identity.get("tsp_mode"):
                self.warn(
                    "the instrument answered a TSP print(); it is in TSP mode, "
                    "so the SCPI candidates below will not apply. Switching "
                    "command sets needs --set-language and a reboot."
                )

        if self.args.set_language:
            self.say(f"  --set-language {self.args.set_language}: sending "
                     f"*LANG {self.args.set_language}")
            self.link.write(f"*LANG {self.args.set_language}")
            identity["language_set_to"] = self.args.set_language
            self.warn("the command set only changes after the instrument is "
                      "rebooted; nothing else in this run reflects it")

        identity["error_after_identity"] = self.error_queue()
        self.report["identity"] = identity

    # -- phase 3: capture the state we have to give back -----------------

    def phase_capture_state(self) -> None:
        self.say("\n[3/8] capturing the configuration to restore")
        assert self.link is not None
        restore = self.profile.get("restore", {})
        for query in restore.get("capture", []):
            value = self.link.query(query)
            if value is not None:
                self.saved_state[query.lstrip(":")] = value
                self.say(f"  {query} -> {value}")
            elif not self.args.dry_run:
                self.warn(f"{query} did not answer; it cannot be restored")
        self.report["saved_state"] = dict(self.saved_state)

    # -- phase 4: the candidate sweep ------------------------------------

    def phase_commands(self) -> None:
        self.say("\n[4/8] candidate command sweep")
        assert self.link is not None
        candidates: List[str] = list(self.profile.get("queries", []))
        if self.args.only:
            wanted = [item.strip() for item in self.args.only.split(",")]
            candidates = [c for c in candidates
                          if any(w.lower() in c.lower() for w in wanted)]
        error_query = self.profile.get("core", {}).get("error")
        for index, command in enumerate(candidates, start=1):
            self.budget("the command sweep")
            record: Dict[str, Any] = {"command": command, "kind": "query"}
            started = time.monotonic()
            try:
                reply = self.link.query(command)
            except ProbeForbidden as exc:
                record.update({"answered": False, "refused": str(exc)})
                self.report["commands"].append(record)
                self.say(f"  {index:3d}/{len(candidates)} REFUSED  {command}")
                continue
            elapsed = round((time.monotonic() - started) * 1000, 1)
            record["ms"] = elapsed
            if reply is None:
                record.update({"answered": False,
                               "timed_out": not self.args.dry_run,
                               "verdict": "NEVER SEND — no answer, link rebuilt"
                               if not self.args.dry_run else "dry run"})
                if not self.args.dry_run:
                    self.say(f"  {index:3d}/{len(candidates)} HUNG     {command}")
            else:
                record.update({"answered": True, "timed_out": False,
                               "reply": reply[:REPLY_CLIP],
                               "verdict": "supported"})
                self.say(f"  {index:3d}/{len(candidates)} ok       "
                         f"{command}  ->  {reply[:80]}")
                if error_query and self.args.check_errors and not self.args.dry_run:
                    record["error_after"] = self.link.query(error_query)
            self.report["commands"].append(record)

        if self.args.sweep_writes:
            for pair in self.profile.get("writes", []):
                self.budget("the write sweep")
                record = {"command": pair["send"], "kind": "write"}
                try:
                    self.link.write(pair["send"])
                except ProbeForbidden as exc:
                    record.update({"answered": False, "refused": str(exc)})
                    self.report["commands"].append(record)
                    continue
                after = self.link.query(error_query) if error_query else None
                record["error_after"] = after
                if pair.get("readback"):
                    record["readback"] = self.link.query(pair["readback"])
                record["answered"] = after is not None and "No error" in (after or "") \
                    or (after or "").lstrip().startswith("0,")
                self.report["commands"].append(record)

    # -- phase 5: functions and ranges -----------------------------------

    def phase_functions(self) -> None:
        self.say("\n[5/8] functions and ranges")
        assert self.link is not None
        if self.args.skip_functions:
            self.say("  skipped (--skip-functions)")
            return
        for spec in self.profile.get("functions", []):
            self.budget("function enumeration")
            entry: Dict[str, Any] = {
                "key": spec["key"], "label": spec.get("label"),
                "short": spec.get("short"), "unit": spec.get("unit"),
                "select": spec.get("select"), "selected": None,
                "readback": None, "ranges": None, "range_min": None,
                "range_max": None, "range_adopted": [], "nplc": {}, "extra": {},
            }
            select = spec.get("select")
            if not select:
                self.report["functions"].append(entry)
                continue
            try:
                self.link.write(select)
            except ProbeForbidden as exc:
                entry["selected"] = False
                entry["detail"] = str(exc)
                self.report["functions"].append(entry)
                continue
            error = self.error_queue()
            entry["error_after_select"] = error
            readback = (self.link.query(spec["readback"])
                        if spec.get("readback") else None)
            entry["readback"] = readback
            entry["selected"] = readback is not None or self.args.dry_run
            self.say(f"  {spec['key']:<12s} select -> {readback}   {error or ''}")

            for label, key in (("min", "range_min"), ("max", "range_max")):
                query = spec.get(key) or (
                    (spec.get("range_query") or "").rstrip("?") + f"? {label.upper()}"
                    if spec.get("range_query") else None)
                if not query:
                    continue
                value = self.link.query(query)
                entry[key] = value
                entry.setdefault("range_queries", {})[key] = query

            derived = derive_ranges(entry.get("range_min"), entry.get("range_max"))
            entry["ranges"] = derived
            if derived is None and self.args.dry_run:
                # Nothing answered, so there is nothing to derive from; show one
                # representative set/read-back so the plan is complete.
                derived = [1.0]
            if derived and spec.get("range_set") and not self.args.skip_range_verify:
                # SPEC.md section 2.4: never trust a range table, always read
                # back what the instrument actually adopted.
                for value in derived:
                    self.budget("range verification")
                    try:
                        self.link.write(spec["range_set"].format(value=repr(value)))
                    except ProbeForbidden:
                        break
                    adopted = self.link.query(spec["range_query"]) \
                        if spec.get("range_query") else None
                    entry["range_adopted"].append(
                        {"requested": value, "adopted": adopted,
                         "error": self.error_queue()})
                    if self.args.dry_run:
                        break
                if spec.get("range_auto"):
                    self.link.write(spec["range_auto"].format(state="ON"))

            for key in ("nplc_query", "nplc_min", "nplc_max", "aperture_query",
                        "resolution_query", "digits_query"):
                query = spec.get(key)
                if query:
                    entry["nplc"][key] = {"command": query,
                                          "reply": self.link.query(query)}
            for query in spec.get("extra", []):
                entry["extra"][query] = self.link.query(query)
            self.report["functions"].append(entry)

    # -- phase 6: throughput ---------------------------------------------

    def phase_throughput(self) -> None:
        self.say("\n[6/8] measurement throughput")
        assert self.link is not None
        plan = self.profile.get("throughput") or {}
        if not plan or self.args.skip_throughput:
            self.say("  skipped")
            return
        for command in plan.get("setup", []):
            self.link.write(command)
        self.error_queue()
        for point in plan.get("points", []):
            self.budget("throughput")
            entry: Dict[str, Any] = {"label": point["label"],
                                     "commands": point["commands"]}
            for command in point["commands"]:
                self.link.write(command)
            entry["error_after_config"] = self.error_queue()
            self.link.write(plan["start"])
            for command in plan.get("start_extra", []):
                self.link.write(command)
            if self.args.dry_run:
                self.link.query(plan["count_query"])
                self._dry_fetch(plan)
                for command in plan.get("stop", []):
                    self.link.write(command)
                self.report["throughput"].append(entry)
                continue

            window = self.args.throughput_seconds
            start_time = time.monotonic()
            first_count = self.link.query(plan["count_query"])
            drained = 0
            cursor = 1
            while time.monotonic() - start_time < window:
                self.budget("throughput")
                count_text = self.link.query(plan["count_query"])
                count = parse_int(count_text)
                if count is None:
                    break
                if plan.get("fetch_indexed"):
                    if count >= cursor:
                        end = min(count, cursor + 4000 - 1)
                        raw = self.link.query_bytes(
                            plan["fetch"].format(start=cursor, end=end),
                            timeout=max(self.args.timeout, 5.0),
                            block=bool(plan.get("fetch_is_block")))
                        if raw is None:
                            break
                        drained += end - cursor + 1
                        cursor = end + 1
                elif count and count > 0:
                    take = min(count, 4000)
                    raw = self.link.query_bytes(
                        plan["fetch"].format(count=take),
                        timeout=max(self.args.timeout, 5.0),
                        block=bool(plan.get("fetch_is_block")))
                    if raw is None:
                        break
                    drained += take
                time.sleep(0.05)
            elapsed = time.monotonic() - start_time
            for command in plan.get("stop", []):
                self.link.write(command)
            entry.update({
                "seconds": round(elapsed, 3),
                "readings": drained,
                "readings_per_second": round(drained / elapsed, 1) if elapsed else None,
                "count_at_start": first_count,
                "error_after": self.error_queue(),
            })
            self.say(f"  {point['label']:<24s} {entry['readings_per_second']} rdg/s "
                     f"({drained} in {elapsed:.1f} s)")
            self.report["throughput"].append(entry)

    def _dry_fetch(self, plan: Dict[str, Any]) -> None:
        template = plan["fetch"]
        if plan.get("fetch_indexed"):
            self.link.query_bytes(template.format(start=1, end=4000),
                                  block=bool(plan.get("fetch_is_block")))
        else:
            self.link.query_bytes(template.format(count=4000),
                                  block=bool(plan.get("fetch_is_block")))

    # -- phase 7: screen capture -----------------------------------------

    def phase_screen(self) -> None:
        self.say("\n[7/8] screen capture")
        assert self.link is not None
        screen = self.profile.get("screen") or {}
        for candidate in screen.get("scpi", []):
            self.budget("screen capture")
            entry: Dict[str, Any] = {"label": candidate["label"],
                                     "query": candidate["query"]}
            for command in candidate.get("setup", []):
                try:
                    self.link.write(command)
                except ProbeForbidden as exc:
                    entry["refused"] = str(exc)
                    break
            started = time.monotonic()
            try:
                data = self.link.query_bytes(
                    candidate["query"],
                    timeout=candidate.get("timeout", 10.0), block=True)
            except ProbeForbidden as exc:
                entry["refused"] = str(exc)
                self.report["screen"]["scpi"].append(entry)
                continue
            entry["seconds"] = round(time.monotonic() - started, 3)
            if data is None:
                entry.update({"supported": False,
                              "detail": "no answer — treat as NEVER SEND"})
                self.say(f"  {candidate['label']:<28s} no answer")
            else:
                entry.update({"supported": True, "bytes": len(data),
                              "format": sniff_image(data),
                              "head": data[:16].hex()})
                self.say(f"  {candidate['label']:<28s} {len(data)} bytes, "
                         f"{entry['format']}, {entry['seconds']} s")
            entry["error_after"] = self.error_queue()
            self.report["screen"]["scpi"].append(entry)

        for path in screen.get("http", []):
            self.budget("screen capture over http")
            entry = {"path": path}
            if self.args.dry_run:
                entry["detail"] = "dry run: would GET this path"
                self.report["screen"]["http"].append(entry)
                self.say(f"  would GET http://{self.host}{path}")
                continue
            url = f"http://{self.host}{path}"
            started = time.monotonic()
            try:
                request = urllib.request.Request(
                    url, headers={"Connection": "close"})
                with urllib.request.urlopen(request, timeout=8.0) as response:
                    body = response.read(2_000_000)
                    entry.update({
                        "status": response.status,
                        "content_type": response.headers.get("Content-Type"),
                        "bytes": len(body),
                        "format": sniff_image(body),
                        "seconds": round(time.monotonic() - started, 3),
                    })
                    entry["is_image"] = entry["format"] != "unknown"
            except urllib.error.HTTPError as exc:
                entry.update({"status": exc.code, "is_image": False})
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                entry.update({"status": None, "is_image": False,
                              "detail": str(exc)})
            self.report["screen"]["http"].append(entry)
            self.say(f"  GET {path:<22s} -> {entry.get('status')} "
                     f"{entry.get('content_type') or ''} "
                     f"{'IMAGE' if entry.get('is_image') else ''}")

    # -- phase 8: crash safety -------------------------------------------

    def phase_crash_safety(self) -> None:
        self.say("\n[8/8] crash safety — does the instrument abort when the "
                 "last session dies?")
        plan = self.profile.get("crash") or {}
        if not plan or self.args.no_crash_test:
            self.say("  skipped")
            return
        if self.args.dry_run:
            self.say("  dry run: a child process would send, then be killed with "
                     "TerminateProcess:")
            for command in plan.get("arm", []):
                self.say(f"    would send {command}")
            self.say("  and then, from a fresh session:")
            for command in plan.get("observe", []):
                self.say(f"    would send {command}")
            for command in plan.get("stop", []):
                self.say(f"    would send {command}   (always, whatever happened)")
            return

        transports = [self.args.transport] if self.args.transport != "auto" \
            else [kind for kind in ("vxi11", "raw")
                  if self._transport_usable(kind)]
        for kind in transports:
            self.budget("the crash-safety test")
            self.say(f"  transport: {kind}")
            result = self._crash_test_one(kind, plan)
            self.report["crash_safety"].append(result)
            verdict = result.get("verdict", "unknown")
            self.say(f"    verdict: {verdict}")

    def _transport_usable(self, kind: str) -> bool:
        transports = self.report.get("transports", {})
        if kind == "vxi11":
            return bool(transports.get("vxi11", {}).get("available"))
        if kind == "raw":
            return bool(transports.get("raw_scpi", {}).get("open"))
        return False

    def _crash_test_one(self, kind: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Arm an acquisition in a child, kill the child outright, then look.

        The child is killed with ``Popen.kill()`` -- ``TerminateProcess`` on
        Windows -- specifically so that no handler, ``atexit`` hook or
        ``finally`` of ours can possibly run.  Anything that stops the
        acquisition after that was the instrument's own doing, which is the
        only kind of deadman that survives a crash.
        """
        result: Dict[str, Any] = {"transport": kind, "verdict": "unknown",
                                  "observations": []}
        arm_path = os.path.join(self.args.out, f".crash-arm-{kind}.json")
        os.makedirs(self.args.out, exist_ok=True)
        with open(arm_path, "w", encoding="utf-8") as handle:
            json.dump({"commands": plan.get("arm", [])}, handle)

        # Stand our own link down first: some instruments allow only one
        # controlling interface at a time, and a link we hold open would also
        # keep any last-link cleanup from firing.
        held = self.link
        if held is not None:
            held.close()

        child = None
        try:
            child = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), self.host,
                 "--crash-child", "--transport", kind, "--arm-file", arm_path,
                 "--timeout", str(self.args.timeout)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            ready = False
            started = time.monotonic()
            while time.monotonic() - started < 30.0:
                line = child.stdout.readline()
                if not line:
                    break
                if line.strip() == "ARMED":
                    ready = True
                    break
                result.setdefault("child_output", []).append(line.rstrip())
            if not ready:
                result["verdict"] = "could not arm the acquisition"
                result.setdefault("child_output", []).append(
                    (child.stderr.read() or "")[:2000])
                return result
            result["armed"] = True
            time.sleep(2.0)
            child.kill()                        # TerminateProcess on Windows
            child.wait(timeout=10.0)
            result["killed_at"] = time.strftime("%H:%M:%S")
        except (OSError, subprocess.SubprocessError) as exc:
            result["verdict"] = f"crash test could not run: {exc}"
            return result
        finally:
            if child is not None and child.poll() is None:
                child.kill()
            try:
                os.remove(arm_path)
            except OSError:
                pass

        # Observe from a genuinely fresh session, twice, a few seconds apart.
        fresh = Link(self.host, kind, timeout=self.args.timeout,
                     never_send=self.profile.get("never_send", ()),
                     verbose=self.args.verbose)
        try:
            time.sleep(2.0)
            fresh.connect()
            for delay in (0.0, 6.0):
                if delay:
                    time.sleep(delay)
                sample = {"at_seconds_after_kill": round(2.0 + delay, 1)}
                for command in plan.get("observe", []):
                    sample[command] = fresh.query(command)
                result["observations"].append(sample)
            result["verdict"] = judge_crash(result["observations"])
        except (ProbeLinkError, ProbeAborted, OSError) as exc:
            result["verdict"] = f"could not observe after the kill: {exc}"
        finally:
            # Always stand the acquisition down, even if the observation failed
            # — leaving one running is the exact fault this phase exists to
            # detect, and it must not be this tool that causes it.
            if fresh.transport is not None:
                for command in plan.get("stop", []):
                    try:
                        fresh.write(command)
                    except (ProbeLinkError, ProbeForbidden, OSError):
                        pass
            fresh.close()

        if held is not None:
            try:
                held.connect()
            except (ProbeLinkError, OSError) as exc:
                self.warn(f"could not reopen the main link after the crash test: {exc}")
        return result

    # -- teardown --------------------------------------------------------

    def teardown(self) -> None:
        """Hand the instrument back: aborted, restored, local, free-running.

        Runs on every exit path.  Each step is independent and a failure is
        recorded rather than allowed to skip the steps after it -- returning
        the meter to local matters more than tidying its trigger count.
        """
        self.say("\nteardown — returning the instrument to local and free-running")
        steps: List[Dict[str, Any]] = []
        link = self.link
        if link is None:
            self.report["teardown"] = [{"step": "none", "detail": "no link was opened"}]
            return
        if link.transport is None and not link.dry_run:
            try:
                link.connect()
            except (ProbeLinkError, OSError) as exc:
                self.report["teardown"] = [
                    {"step": "reconnect", "ok": False, "detail": str(exc)}]
                self.say(f"  could not reconnect to restore the instrument: {exc}")
                return

        def run(step: str, action: Callable[[], Any]) -> None:
            entry: Dict[str, Any] = {"step": step}
            try:
                entry["reply"] = action()
                entry["ok"] = True
            except Exception as exc:                      # every failure surfaces
                entry["ok"] = False
                entry["detail"] = f"{type(exc).__name__}: {exc}"
            steps.append(entry)
            marker = "ok" if entry["ok"] else "FAILED"
            self.say(f"  {step:<46s} {marker}")

        abort = self.profile.get("core", {}).get("abort")
        if abort:
            run(f"abort the acquisition ({abort})", lambda: link.write(abort))

        restore = self.profile.get("restore", {})
        for template in restore.get("apply", []):
            command = expand_template(template, self.saved_state)
            if command is None:
                steps.append({"step": template, "ok": False,
                              "detail": "the value to restore was never captured"})
                self.say(f"  {template:<46s} skipped (never captured)")
                continue
            run(f"restore {command}", lambda cmd=command: link.write(cmd))

        # Drain the error queue BEFORE handing back, never after: on many
        # instruments any command over LAN puts them straight back into remote,
        # so a check placed after the hand-back undoes it (IO-DISCIPLINE.md
        # rule 6).
        error = self.profile.get("core", {}).get("error")
        if error:
            run("drain the error queue (before the hand-back)",
                lambda: link.query(error))

        for command in self.profile.get("core", {}).get("local", []):
            run(f"return to local ({command})", lambda cmd=command: link.write(cmd))

        run("close the link", lambda: link.close())
        self.report["teardown"] = steps

    # -- driver ----------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        self.say(f"probing {self.host}"
                 f"{'  (DRY RUN — nothing is sent)' if self.args.dry_run else ''}")
        if not self.args.dry_run:
            self.canary.establish()
            base = (f"{self.canary.baseline_ms} ms" if self.canary.baseline_ms
                    else "unavailable")
            self.say(f"  health canary: {self.canary.mode}, baseline {base}")
            if self.canary.mode == "none":
                self.warn("no health canary is available on this instrument "
                          "(no web server, no HTTP port). The run continues, but "
                          "nothing will notice its LAN stack degrading.")

        try:
            self.phase_transports()

            # Choose a profile.  With --profile auto this needs an *IDN?, so a
            # throwaway generic link asks for one first.
            profile_name = self.args.profile
            idn: Optional[str] = None
            if self.args.candidates:
                self.profile = load_candidates(self.args.candidates)
            else:
                if profile_name == "auto" and not self.args.dry_run:
                    idn = self._quick_idn()
                self.profile = select_profile(profile_name, idn)
            self.report["profile"] = {
                "name": self.profile.get("name"),
                "description": self.profile.get("description"),
                "source": self.profile.get("source"),
                "verified": bool(self.profile.get("verified")),
                "never_send": list(self.profile.get("never_send", [])),
                "candidate_queries": len(self.profile.get("queries", [])),
                "selected_by": "candidates file" if self.args.candidates
                else ("auto from *IDN?" if profile_name == "auto" else "--profile"),
            }
            self.say(f"\nprofile: {self.profile['name']} — "
                     f"{self.profile.get('description')}")
            if not self.profile.get("verified"):
                self.say("  NOTE: this profile's commands are UNVERIFIED "
                         "candidates. That is what this run is for.")

            kind = self.args.transport
            if kind == "auto":
                kind = "vxi11" if self._transport_usable("vxi11") else "raw"
                self.say(f"  transport: auto -> {kind}")
            self.link = Link(self.host, kind, timeout=self.args.timeout,
                             never_send=self.profile.get("never_send", ()),
                             dry_run=self.args.dry_run, verbose=self.args.verbose,
                             ops_per_second=self.args.ops_per_second)
            self.link.connect()
            if not self.args.dry_run:
                self.say(f"  link: {self.link.describe}")

            self.phase_identity()
            self.gap("identity")
            self.phase_capture_state()
            self.gap("capture")
            self.phase_commands()
            self.gap("commands")
            self.phase_functions()
            self.gap("functions")
            self.phase_throughput()
            self.gap("throughput")
            self.phase_screen()
            self.gap("screen")
            self.phase_crash_safety()
        except ProbeAborted as exc:
            self.report["aborted"] = str(exc)
            self.say(f"\nABORTED: {exc}")
        except KeyboardInterrupt:
            self.report["aborted"] = "interrupted from the keyboard"
            self.say("\nINTERRUPTED")
        except Exception as exc:                          # never swallowed
            self.report["aborted"] = f"{type(exc).__name__}: {exc}"
            self.say(f"\nFAILED: {type(exc).__name__}: {exc}")
        finally:
            try:
                self.teardown()
            except Exception as exc:
                self.report["teardown"].append(
                    {"step": "teardown", "ok": False,
                     "detail": f"{type(exc).__name__}: {exc}"})
                self.say(f"  teardown itself failed: {exc}")
            self.report["canary"] = {
                "mode": self.canary.mode,
                "baseline_ms": self.canary.baseline_ms,
                "degraded": self.canary.degraded_reason,
                "samples": self.canary.samples,
            }
            self.report["meta"]["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.report["meta"]["elapsed_seconds"] = round(time.time() - self.started, 1)
            if self.link is not None:
                self.report["meta"]["operations"] = self.link.ops
                self.report["meta"]["timeouts"] = self.link.timeouts
                self.report["meta"]["link_rebuilds"] = self.link.rebuilds
                self.report["meta"]["commands_planned"] = len(self.link.sent)
        return self.report

    def _quick_idn(self) -> Optional[str]:
        """One throwaway session, one command, so --profile auto has something
        to match on.  Uses whichever transport is available."""
        kind = "vxi11" if self._transport_usable("vxi11") else "raw"
        link = Link(self.host, kind, timeout=min(self.args.timeout, 3.0))
        try:
            link.connect()
            return link.query("*IDN?")
        except (ProbeLinkError, ProbeAborted, OSError):
            return None
        finally:
            link.close()


# ==========================================================================
# Small pure helpers
# ==========================================================================


def parse_float(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_int(text: Optional[str]) -> Optional[int]:
    value = parse_float(text)
    return None if value is None else int(value)


def derive_ranges(minimum: Optional[str], maximum: Optional[str]) -> Optional[List[float]]:
    """SPEC.md section 2.4: multiply MIN by ten until MAX, then append MAX.

    Returned as a *candidate* list.  The caller sets each one and reads back
    what the instrument adopted, because this rule is an inference and the
    readback is the measurement.
    """
    low, high = parse_float(minimum), parse_float(maximum)
    if low is None or high is None or low <= 0 or high < low:
        return None
    values: List[float] = []
    value = low
    for _ in range(32):
        if value >= high:
            break
        values.append(round(value, 12))
        value *= 10.0
    values.append(round(high, 12))
    return values


def sniff_image(data: bytes) -> str:
    if data.startswith(b"BM"):
        return "BMP"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if data.startswith(b"<?xml") or data.startswith(b"<svg"):
        return "SVG"
    return "unknown"


def judge_crash(observations: Sequence[Dict[str, Any]]) -> str:
    """Turn the two post-kill samples into a verdict about the acquisition.

    Deliberately conservative: it says "unknown" whenever the evidence does not
    settle the question, because the whole point of measuring this is that it
    must not be assumed.
    """
    if len(observations) < 2:
        return "unknown — not enough samples"
    counts: List[Optional[int]] = []
    states: List[Optional[str]] = []
    for sample in observations:
        for key, value in sample.items():
            if key == "at_seconds_after_kill":
                continue
            if "ACTual" in key or "DATA:POIN" in key.upper():
                counts.append(parse_int(value))
            if "STATe" in key or "COND" in key.upper():
                states.append(value)
    if len(counts) >= 2 and counts[0] is not None and counts[-1] is not None:
        if counts[-1] > counts[0]:
            return ("NOT crash safe — readings were still accumulating "
                    f"({counts[0]} -> {counts[-1]}) after the client was killed")
        if counts[-1] == 0 and counts[0] == 0:
            return ("crash safe — reading memory was empty in both samples, so "
                    "the instrument cleared it when the session died")
        return (f"stopped, but memory was not cleared (count held at {counts[-1]}); "
                "the acquisition ended, the data did not go away")
    if states and states[0]:
        upper = (states[0] or "").upper()
        if "RUNNING" in upper:
            return "NOT crash safe — the trigger model was still RUNNING after the kill"
        if "IDLE" in upper or "ABORTED" in upper:
            return f"crash safe — the trigger model was {states[0]!r} after the kill"
    return "unknown — the observations did not settle it"


def expand_template(template: str, saved: Dict[str, str]) -> Optional[str]:
    """Fill ``{SENSe:FUNCtion?}`` style placeholders from the captured state."""
    out = template
    for match in re.findall(r"\{([^{}]+)\}", template):
        value = saved.get(match) or saved.get(match.lstrip(":"))
        if value is None:
            return None
        out = out.replace("{" + match + "}", value.strip())
    return out


# ==========================================================================
# Reports
# ==========================================================================


def write_reports(report: Dict[str, Any], out_dir: str, host: str) -> Tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_host = re.sub(r"[^A-Za-z0-9_.-]", "_", host)
    json_path = os.path.join(out_dir, f"probe-{safe_host}-{stamp}.json")
    md_path = os.path.join(out_dir, f"probe-{safe_host}-{stamp}.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=False, default=str)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))
    return json_path, md_path


def render_markdown(report: Dict[str, Any]) -> str:
    """Render the findings in the shape of SPEC.md sections 1 and 2.

    Deliberately the same shape, so the result drops into a driver table
    without a translation step: a facts table, then SUPPORTED, then
    NEVER SEND, then the range enumeration.
    """
    meta = report.get("meta", {})
    identity = report.get("identity", {})
    profile = report.get("profile", {})
    lines: List[str] = []
    add = lines.append

    add(f"# Probe report — {meta.get('host')}")
    add("")
    add(f"Measured by `tools/probe_instrument.py` {meta.get('tool_version')} on "
        f"{meta.get('started')}, finished {meta.get('finished')} "
        f"({meta.get('elapsed_seconds')} s, {meta.get('operations', 0)} operations, "
        f"{meta.get('timeouts', 0)} timeouts, "
        f"{meta.get('link_rebuilds', 0)} link rebuilds).")
    add("")
    if meta.get("dry_run"):
        add("> **DRY RUN.** Nothing below was sent. This is the plan, not a result.")
        add("")
    add(f"Candidate profile: **{profile.get('name')}** — {profile.get('description')}")
    add("")
    add(f"Profile source: {profile.get('source')}")
    add("")
    if not profile.get("verified"):
        add("> The profile's commands were candidates, not facts. Everything in "
            "section 2 below is now a *measurement* of this instrument, and "
            "supersedes the candidate list it came from.")
        add("")
    if report.get("aborted"):
        add(f"> **The run did not complete: {report['aborted']}** Treat every "
            "section below as partial.")
        add("")

    # ---- section 1 ----
    add("## 1. Instrument facts")
    add("")
    add("| Fact | Value |")
    add("|---|---|")
    add(f"| IDN | `{identity.get('idn')}` |")
    if identity.get("model"):
        add(f"| Model | {identity.get('model')} |")
        add(f"| Serial | {identity.get('serial')} |")
        add(f"| Firmware | {identity.get('firmware')} |")
    if identity.get("language") is not None:
        add(f"| Command language (`{identity.get('language_query')}`) | "
            f"`{identity.get('language')}` |")
    if identity.get("tsp_mode") is not None:
        add(f"| Answers a TSP `print()` | {identity.get('tsp_mode')} |")

    transports = report.get("transports", {})
    for label, key in (("Raw socket 5025", "raw_scpi"), ("Telnet 23", "telnet"),
                       ("Portmapper 111", "portmapper"),
                       ("VXI-11 direct 1024", "vxi11_direct"),
                       ("HiSLIP 4880", "hislip"),
                       ("Dead socket 5030", "dead_socket"),
                       ("HTTP 80", "http")):
        entry = transports.get(key)
        if isinstance(entry, dict):
            add(f"| {label} | {'open' if entry.get('open') else 'closed'} |")
    vxi = transports.get("vxi11")
    if isinstance(vxi, dict):
        add(f"| VXI-11 core channel | "
            f"{'yes, port ' + str(vxi.get('core_port')) if vxi.get('available') else 'no'} "
            f"({vxi.get('port_source') or vxi.get('detail')}) |")
    hislip = transports.get("hislip_detail")
    if isinstance(hislip, dict):
        add(f"| HiSLIP Initialize | "
            f"{'negotiated ' + str(hislip.get('protocol_version')) if hislip.get('available') else 'no'} |")

    canary = report.get("canary", {})
    add(f"| Health canary | {canary.get('mode')}, baseline "
        f"{canary.get('baseline_ms')} ms, "
        f"{'DEGRADED: ' + canary['degraded'] if canary.get('degraded') else 'stayed healthy'} |")
    add("")

    # ---- crash safety ----
    add("### Crash safety — what happens when the last session dies")
    add("")
    crash = report.get("crash_safety") or []
    if not crash:
        add("Not measured in this run.")
    else:
        add("| Transport | Verdict |")
        add("|---|---|")
        for entry in crash:
            add(f"| {entry.get('transport')} | {entry.get('verdict')} |")
        add("")
        add("Method: a child process armed a continuous acquisition and was then "
            "ended with `TerminateProcess`, so no handler of ours could run. "
            "The observations below come from a fresh session opened afterwards.")
        add("")
        for entry in crash:
            for sample in entry.get("observations", []):
                pieces = ", ".join(f"`{k}` = `{v}`" for k, v in sample.items()
                                   if k != "at_seconds_after_kill")
                add(f"- {entry.get('transport')}, "
                    f"t+{sample.get('at_seconds_after_kill')} s: {pieces}")
    add("")

    # ---- throughput ----
    throughput = report.get("throughput") or []
    if throughput:
        add("### Measurement throughput")
        add("")
        add("| Integration | Readings/s | Readings | Seconds | Error queue after |")
        add("|---|---|---|---|---|")
        for entry in throughput:
            add(f"| {entry.get('label')} | {entry.get('readings_per_second')} | "
                f"{entry.get('readings')} | {entry.get('seconds')} | "
                f"`{entry.get('error_after')}` |")
        add("")

    # ---- screen ----
    screen = report.get("screen", {})
    if screen.get("scpi") or screen.get("http"):
        add("### Screen capture")
        add("")
        if screen.get("scpi"):
            add("| Candidate | Supported | Bytes | Format | Seconds |")
            add("|---|---|---|---|---|")
            for entry in screen["scpi"]:
                add(f"| `{entry.get('query')}` | {entry.get('supported')} | "
                    f"{entry.get('bytes', '')} | {entry.get('format', '')} | "
                    f"{entry.get('seconds', '')} |")
            add("")
        images = [entry for entry in screen.get("http", []) if entry.get("is_image")]
        if images:
            add("HTTP endpoints that returned an image:")
            add("")
            for entry in images:
                add(f"- `http://{meta.get('host')}{entry['path']}` — "
                    f"{entry.get('bytes')} bytes, {entry.get('format')}, "
                    f"`{entry.get('content_type')}`")
        elif screen.get("http"):
            add("No HTTP endpoint tried returned an image. Tried: "
                + ", ".join(f"`{entry['path']}`" for entry in screen["http"]))
        add("")

    # ---- section 2 ----
    add("## 2. Command set")
    add("")
    commands = report.get("commands") or []
    if meta.get("dry_run"):
        add("A dry run establishes nothing about the instrument. These are the "
            f"{len(commands)} candidates that would have been sent, one at a "
            "time, each followed by a link rebuild if it did not answer:")
        add("")
        for entry in commands:
            add(f"- `{entry['command']}`")
        add("")
        add("---")
        add("")
        return "\n".join(lines)

    supported = [entry for entry in commands if entry.get("answered")]
    hung = [entry for entry in commands if entry.get("timed_out")]
    refused = [entry for entry in commands if entry.get("refused")]

    add("### 2.1 SUPPORTED — verified to answer, in this run, on this unit")
    add("")
    if supported:
        add("| Command | Reply | ms | Error queue after |")
        add("|---|---|---|---|")
        for entry in supported:
            reply = (entry.get("reply") or "").replace("|", "\\|")
            add(f"| `{entry['command']}` | `{reply}` | {entry.get('ms')} | "
                f"`{entry.get('error_after', '')}` |")
    else:
        add("Nothing answered.")
    add("")

    add("### 2.2 UNSUPPORTED — NEVER SEND (each one hung the link)")
    add("")
    if hung:
        add("Each of these was sent once, did not answer within the timeout, and "
            "the link was torn down and rebuilt before the next candidate. "
            "Treat this list the way `SPEC.md` section 2.2 is treated: it is not "
            "theoretical.")
        add("")
        for entry in hung:
            add(f"- `{entry['command']}`")
    else:
        add("Nothing hung. That is a real result, but a short candidate list is "
            "also a way to get it — check how many candidates the profile had.")
    add("")
    if refused:
        add("Refused by the profile's never-send list, so never actually sent:")
        add("")
        for entry in refused:
            add(f"- `{entry['command']}`")
        add("")

    # ---- functions ----
    functions = report.get("functions") or []
    if functions:
        add("### 2.3 Functions and ranges")
        add("")
        add("Ranges are enumerated at runtime from `MIN`/`MAX` and then verified "
            "by setting each one and reading back what the instrument adopted. "
            "No table here is hardcoded.")
        add("")
        add("| Function | Selects | Readback | MIN | MAX | Derived ranges |")
        add("|---|---|---|---|---|---|")
        for entry in functions:
            ranges = entry.get("ranges")
            shown = ", ".join(format_si(v) for v in ranges) if ranges else "—"
            add(f"| {entry.get('key')} | {entry.get('selected')} | "
                f"`{entry.get('readback')}` | `{entry.get('range_min')}` | "
                f"`{entry.get('range_max')}` | {shown} |")
        add("")
        for entry in functions:
            adopted = entry.get("range_adopted") or []
            if not adopted:
                continue
            mismatched = [item for item in adopted
                          if parse_float(item.get("adopted")) is not None
                          and abs(parse_float(item["adopted"]) - item["requested"])
                          > abs(item["requested"]) * 1e-6]
            if mismatched:
                add(f"`{entry['key']}` — the instrument adopted a different range "
                    "than was asked for, which is the whole reason for reading back:")
                add("")
                for item in mismatched:
                    add(f"- asked `{item['requested']}`, adopted "
                        f"`{item['adopted']}`")
                add("")

    # ---- teardown ----
    add("### 2.4 Teardown")
    add("")
    add("| Step | Result | Detail |")
    add("|---|---|---|")
    for entry in report.get("teardown", []):
        add(f"| {entry.get('step')} | {'ok' if entry.get('ok') else 'FAILED'} | "
            f"{entry.get('detail', '')} |")
    add("")

    warnings = report.get("warnings") or []
    if warnings:
        add("## Warnings raised during the run")
        add("")
        for warning in warnings:
            add(f"- {warning}")
        add("")

    add("---")
    add("")
    add("Nothing in this report is inferred from a datasheet. Every row is "
        "either something this instrument answered, or something it did not.")
    add("")
    return "\n".join(lines)


def format_si(value: float) -> str:
    for factor, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
                           (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= factor:
            scaled = value / factor
            text = f"{scaled:g}"
            return f"{text}{suffix}"
    return f"{value:g}"


# ==========================================================================
# The crash-test child
# ==========================================================================


def run_crash_child(args: argparse.Namespace) -> int:
    """Open a link, arm an acquisition, say ARMED, then wait to be killed.

    Nothing here cleans up on purpose.  The parent kills this process outright,
    and whether the acquisition stops is then entirely the instrument's
    business -- which is precisely the question.
    """
    with open(args.arm_file, "r", encoding="utf-8") as handle:
        plan = json.load(handle)
    link = Link(args.host, args.transport, timeout=args.timeout)
    link.connect()
    for command in plan.get("commands", []):
        link.write(command)
        time.sleep(0.05)
    print("ARMED", flush=True)
    while True:
        time.sleep(60)


# ==========================================================================
# Entry point
# ==========================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Characterise an unknown LAN instrument by measuring it.",
        epilog=PROFILE_SCHEMA_NOTE,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", nargs="?", help="instrument hostname or IP")
    parser.add_argument("--profile", default="auto",
                        help="candidate list to use: "
                             + ", ".join(sorted(PROFILES)) + ", or auto")
    parser.add_argument("--candidates",
                        help="JSON file with a candidate list; overrides --profile")
    parser.add_argument("--list-profiles", action="store_true",
                        help="print the built-in profiles and exit")
    parser.add_argument("--transport", default="auto",
                        choices=("auto", "vxi11", "raw"))
    parser.add_argument("--out", default="probe-reports",
                        help="directory for the JSON and Markdown reports")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"seconds before a command counts as a hang "
                             f"(default {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--ops-per-second", type=int, default=MAX_OPS_PER_SECOND,
                        help=f"link-level ceiling (default {MAX_OPS_PER_SECOND})")
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
                        help=f"whole-run budget (default {DEFAULT_MAX_SECONDS:g})")
    parser.add_argument("--throughput-seconds", type=float, default=4.0,
                        help="how long each throughput point runs (default 4)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print exactly what would be sent and send nothing")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--only", help="substring filter for the command sweep")
    parser.add_argument("--check-errors", action="store_true", default=True,
                        help="read the error queue after every candidate (default)")
    parser.add_argument("--no-check-errors", dest="check_errors",
                        action="store_false")
    parser.add_argument("--no-canary", action="store_true",
                        help="do not run the HTTP health canary (not recommended)")
    parser.add_argument("--no-crash-test", action="store_true",
                        help="skip the hard-kill crash-safety phase")
    parser.add_argument("--skip-functions", action="store_true")
    parser.add_argument("--skip-throughput", action="store_true")
    parser.add_argument("--skip-range-verify", action="store_true",
                        help="enumerate ranges but do not set and read each back")
    parser.add_argument("--sweep-writes", action="store_true",
                        help="also try the profile's candidate writes")
    parser.add_argument("--tsp-probe", action="store_true", default=True,
                        help="send one TSP print() to detect the command set "
                             "(read-only, never switches; default on)")
    parser.add_argument("--no-tsp-probe", dest="tsp_probe", action="store_false")
    parser.add_argument("--set-language", choices=("SCPI", "TSP"),
                        help="EXPLICIT opt-in: send *LANG. The instrument needs "
                             "a reboot afterwards. Never done automatically.")
    # internal
    parser.add_argument("--crash-child", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--arm-file", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_profiles:
        for name, profile in sorted(PROFILES.items()):
            mark = "verified" if profile.get("verified") else "UNVERIFIED"
            print(f"{name:22s} [{mark}] {profile.get('description')}")
            print(f"{'':22s} source: {profile.get('source')}")
            print(f"{'':22s} {len(profile.get('queries', []))} candidate queries, "
                  f"{len(profile.get('functions', []))} functions, "
                  f"{len(profile.get('never_send', []))} never-send entries")
        return 0

    if not args.host:
        parser.error("an instrument host is required")

    if args.crash_child:
        return run_crash_child(args)

    probe = Probe(args)
    report = probe.run()
    json_path, md_path = write_reports(report, args.out, args.host)
    print(f"\nreports written:\n  {json_path}\n  {md_path}")
    return 0 if not report.get("aborted") else 2


if __name__ == "__main__":
    raise SystemExit(main())
