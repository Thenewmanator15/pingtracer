#!/usr/bin/env python3
"""Measures forward and backward latency separately, using a program at each end.

An ordinary ping only gives the round trip, because only the sender holds a
clock.  Here both ends stamp the time:

    1. the sender notes the time and sends a probe
    2. the reflector notes when the probe arrived
    3. the reflector notes when it sends the reply
    4. the sender notes when the reply arrived

    forward  = 2 - 1        backward = 4 - 3        held = 3 - 2

On the far machine (any Python 3.8 or newer; nothing else needed):

    python ping_twoway.py reflect

On this machine:

    py -3.15 ping_twoway.py send 192.168.1.233 --count 600 --rate 5 --csv twoway.csv

The CSV has the same shape as a PingTracer export, with the three directions
written as three "hosts" (forward, backward, round trip), so ping_report.py,
ping_histogram.py and ping_spikes.py all work on it as they are.

What to trust.  Times 2 and 3 come from the other machine's clock.  If that
clock is 5 ms ahead of this one, forward reads 5 ms long and backward 5 ms
short; the round trip is unaffected.  So:

* loss in each direction, jitter in each direction, and which direction a
  spike was in are valid whatever the clocks say, because a constant clock
  error cancels out of any change;
* the actual milliseconds each way are only as good as the agreement between
  the two clocks.  Give a known difference with --clock-offset-ms.

The sender prints what the clock difference would be if the path were the
same both ways.  It cannot tell a clock difference from a real asymmetry;
nothing using only these four times can.

The --hold-* and --drop-* options on the reflector exist to test this script:
they add a known delay or loss in one direction.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import socket
import struct
import sys
import threading
import time
from datetime import datetime

MAGIC = b"PT2W"
PROBE = struct.Struct("!4sQIq")        # magic, session, sequence, t1
REPLY = struct.Struct("!4sQIqqqI")     # magic, session, sequence, t1, t2, t3, probes received so far
DEFAULT_PORT = 47800
FINAL_COUNT = 0xFFFFFFFF               # sequence number of the closing request for the reflector's count
TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def now_ns() -> int:
    """Wall-clock time in nanoseconds.  Wall-clock, because it has to be compared with another machine's."""
    return time.time_ns()


# ---------------------------------------------------------------------- reflector


class Reflector:
    """Answers probes, stamping when each arrived and when its reply left."""

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT, hold_forward_ms: float = 0.0, hold_back_ms: float = 0.0,
                 drop_forward_every: int = 0, drop_back_every: int = 0) -> None:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.hold_forward = hold_forward_ms / 1000.0
        self.hold_back = hold_back_ms / 1000.0
        self.drop_forward_every = drop_forward_every
        self.drop_back_every = drop_back_every
        self.received: dict[tuple, int] = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None

    def handle(self, data: bytes, address: tuple) -> None:
        if len(data) < PROBE.size:
            return
        magic, session, sequence, t1 = PROBE.unpack_from(data)
        if magic != MAGIC:
            return
        if sequence == FINAL_COUNT:
            # The sender's closing question: how many probes arrived?  Never counted, held or dropped.
            with self.lock:
                count = self.received.get((address, session), 0)
            stamp = now_ns()
            try:
                self.sock.sendto(REPLY.pack(MAGIC, session, sequence, t1, stamp, stamp, count), address)
            except OSError:
                pass
            return
        # Test hooks: a probe "lost on the way here" is never counted; one "delayed on the way here" is stamped late.
        if self.drop_forward_every and (sequence + 1) % self.drop_forward_every == 0:
            return
        if self.hold_forward:
            time.sleep(self.hold_forward)
        t2 = now_ns()
        with self.lock:
            key = (address, session)
            count = self.received.get(key, 0) + 1
            self.received[key] = count
        if self.drop_back_every and (sequence + 1) % self.drop_back_every == 0:
            return
        t3 = now_ns()
        reply = REPLY.pack(MAGIC, session, sequence, t1, t2, t3, count) + data[PROBE.size:]
        hold_back = self.hold_back_seconds(sequence)
        if hold_back:
            time.sleep(hold_back)   # after the stamp, so it looks like delay on the way back
        try:
            self.sock.sendto(reply, address)
        except OSError:
            pass

    def hold_back_seconds(self, sequence: int) -> float:
        """Testing: how long to delay this reply.  A test can override this to make the delay vary."""
        return self.hold_back

    def serve(self) -> None:
        delayed = bool(self.hold_forward or self.hold_back)
        while not self.stopping.is_set():
            try:
                data, address = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if delayed:
                # Each held probe waits on its own thread so that one does not delay the next.
                threading.Thread(target=self.handle, args=(data, address), daemon=True).start()
            else:
                self.handle(data, address)

    def start(self) -> "Reflector":
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.sock.close()


# ------------------------------------------------------------------------- sender


class Probe:
    __slots__ = ("sequence", "t1", "t2", "t3", "t4")

    def __init__(self, sequence: int, t1: int) -> None:
        self.sequence, self.t1 = sequence, t1
        self.t2 = self.t3 = self.t4 = None

    @property
    def answered(self) -> bool:
        return self.t4 is not None


class Result:
    def __init__(self, host: str, probes: list[Probe], reflector_received: int, clock_offset_ms: float) -> None:
        self.host = host
        self.probes = probes
        self.reflector_received = reflector_received
        self.clock_offset_ms = clock_offset_ms
        # True once the reflector has confirmed its final count.  Without that, losses at the very end may be
        # attributed to the wrong direction.
        self.count_confirmed = False

    def forward_ms(self, p: Probe) -> float:
        return (p.t2 - p.t1) / 1e6 - self.clock_offset_ms

    def backward_ms(self, p: Probe) -> float:
        return (p.t4 - p.t3) / 1e6 + self.clock_offset_ms

    @staticmethod
    def held_ms(p: Probe) -> float:
        return (p.t3 - p.t2) / 1e6

    @staticmethod
    def round_trip_ms(p: Probe) -> float:
        """Time on the network both ways: the total less the time the reflector held the probe."""
        return (p.t4 - p.t1) / 1e6 - (p.t3 - p.t2) / 1e6

    @property
    def sent(self) -> int:
        return len(self.probes)

    @property
    def answered(self) -> int:
        return sum(1 for p in self.probes if p.answered)

    @property
    def lost_forward(self) -> int:
        """Probes the reflector never saw."""
        return max(0, self.sent - self.reflector_received) if self.answered else self.sent

    @property
    def lost_backward(self) -> int:
        """Probes the reflector saw and answered, whose reply never arrived."""
        return max(0, self.reflector_received - self.answered)

    def symmetric_clock_offset_ms(self) -> float:
        """What the clock difference would be if the path took equally long each way (the NTP assumption)."""
        values = [((p.t2 - p.t1) - (p.t4 - p.t3)) / 2e6 for p in self.probes if p.answered]
        return median(values)


def median(values: list[float]) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def mean_change(values: list[float]) -> float:
    """Jitter: the mean change from one value to the next."""
    if len(values) < 2:
        return math.nan
    return sum(abs(b - a) for a, b in zip(values, values[1:])) / (len(values) - 1)


def send(host: str, port: int = DEFAULT_PORT, count: int = 100, rate: float = 5.0, size: int = 64, timeout: float = 2.0,
         clock_offset_ms: float = 0.0, progress: bool = False) -> Result:
    info = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
    sock = socket.socket(info[0], socket.SOCK_DGRAM)
    sock.settimeout(0.05)
    address = info[4]
    session = random.getrandbits(63)
    padding = bytes(max(0, size - PROBE.size))
    probes: list[Probe] = []
    highest_count = 0
    stop = threading.Event()
    final_count = threading.Event()

    def receive() -> None:
        nonlocal highest_count
        while not stop.is_set():
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                # Windows reports "connection reset" on a UDP socket when nothing is listening at the far end.
                continue
            t4 = now_ns()
            if len(data) < REPLY.size:
                continue
            magic, reply_session, sequence, t1, t2, t3, received = REPLY.unpack_from(data)
            if magic != MAGIC or reply_session != session:
                continue
            if sequence == FINAL_COUNT:
                highest_count = max(highest_count, received)
                final_count.set()
                continue
            if sequence >= len(probes):
                continue
            probe = probes[sequence]
            if probe.t4 is None and probe.t1 == t1:
                probe.t2, probe.t3, probe.t4 = t2, t3, t4
                highest_count = max(highest_count, received)

    receiver = threading.Thread(target=receive, daemon=True)
    receiver.start()
    interval = 1.0 / rate
    started = time.perf_counter()
    try:
        for sequence in range(count):
            # Sent on a fixed timeline, so a late wake-up does not push back every later probe.
            wait = started + sequence * interval - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            probe = Probe(sequence, now_ns())
            probes.append(probe)
            try:
                sock.sendto(PROBE.pack(MAGIC, session, sequence, probe.t1) + padding, address)
            except OSError:
                pass
            if progress and (sequence + 1) % max(1, int(rate * 10)) == 0:
                print(f"  sent {sequence + 1} of {count}", file=sys.stderr)
        time.sleep(timeout)
        # Each reply carries the reflector's running count, but if the last replies were lost the count seen here is
        # stale and their loss would be blamed on the wrong direction.  So ask for the final count, a few times over.
        for _ in range(5):
            try:
                sock.sendto(PROBE.pack(MAGIC, session, FINAL_COUNT, now_ns()), address)
            except OSError:
                pass
            if final_count.wait(0.3):
                break
    finally:
        stop.set()
        receiver.join(timeout=1)
        sock.close()
    result = Result(host, probes, highest_count, clock_offset_ms)
    result.count_confirmed = final_count.is_set()
    return result


def write_csv(result: Result, path: str) -> None:
    """Writes the result as a PingTracer-style export, each direction as its own host."""
    directions = (
        ("forward", result.forward_ms),
        ("backward", result.backward_ms),
        ("round trip", result.round_trip_ms),
    )
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Host", "Sent", "Received", "Result", "RoundTripMs", "MeasuredMs", "PayloadBytes", "SentData", "ReceivedData"])
        for name, value in directions:
            for p in result.probes:
                sent = datetime.fromtimestamp(p.t1 / 1e9).strftime(TIME_FORMAT)
                if p.answered:
                    received = datetime.fromtimestamp(p.t4 / 1e9).strftime(TIME_FORMAT)
                    writer.writerow([f"{result.host} {name}", sent, received, "Success", f"{value(p):.3f}", f"{(p.t4 - p.t1) / 1e6:.3f}", "", "", ""])
                else:
                    writer.writerow([f"{result.host} {name}", sent, "", "TimedOut", "", "", "", "", ""])


def summary(result: Result) -> str:
    done = [p for p in result.probes if p.answered]
    lines = [f"== {result.host}: {result.sent} probes sent, {result.answered} answered"]
    lines.append(f"  lost on the way out   {result.lost_forward}   (the reflector counted {result.reflector_received} arriving)")
    lines.append(f"  lost on the way back  {result.lost_backward}")
    if result.answered and not result.count_confirmed:
        lines.append("  (the reflector did not confirm its final count, so losses at the very end may be in the wrong row)")
    if not done:
        lines.append("  no replies: is the reflector running, and is the port open?")
        return "\n".join(lines)
    for name, value in (("forward", result.forward_ms), ("backward", result.backward_ms), ("round trip", result.round_trip_ms)):
        values = [value(p) for p in done]
        lines.append(f"  {name:<11} median {median(values):9.3f} ms   lowest {min(values):9.3f}   highest {max(values):9.3f}   jitter {mean_change(values):7.3f}")
    lines.append(f"  held at the far end: median {median([result.held_ms(p) for p in done]):.3f} ms")
    offset = result.symmetric_clock_offset_ms()
    lines.append(f"  if the path took equally long each way, the far clock would be {offset:+.3f} ms from this one")
    if result.clock_offset_ms:
        lines.append(f"  forward and backward above were corrected for a stated clock difference of {result.clock_offset_ms:+.3f} ms")
    else:
        lines.append("  forward and backward above include any difference between the two clocks; their jitter and the losses do not")
    return "\n".join(lines)


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure forward and backward latency with a program at each end.")
    modes = parser.add_subparsers(dest="mode", required=True)

    reflect = modes.add_parser("reflect", help="run on the far machine: answers probes")
    reflect.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: all IPv4; use :: for IPv6)")
    reflect.add_argument("--port", type=int, default=DEFAULT_PORT)
    reflect.add_argument("--hold-forward-ms", type=float, default=0.0, help="testing: pretend probes took this much longer to arrive")
    reflect.add_argument("--hold-back-ms", type=float, default=0.0, help="testing: delay replies by this much")
    reflect.add_argument("--drop-forward-every", type=int, default=0, help="testing: lose every Nth probe on the way in")
    reflect.add_argument("--drop-back-every", type=int, default=0, help="testing: lose every Nth reply on the way out")

    sender = modes.add_parser("send", help="run on this machine: sends probes and reports")
    sender.add_argument("host", help="the machine running the reflector")
    sender.add_argument("--port", type=int, default=DEFAULT_PORT)
    sender.add_argument("--count", type=int, default=100, help="probes to send (default 100)")
    sender.add_argument("--rate", type=float, default=5.0, help="probes per second (default 5)")
    sender.add_argument("--size", type=int, default=64, help="probe size in bytes (default 64)")
    sender.add_argument("--timeout", type=float, default=2.0, help="seconds to wait for the last replies (default 2)")
    sender.add_argument("--clock-offset-ms", type=float, default=0.0, help="how far the far clock is ahead of this one, if known")
    sender.add_argument("--csv", help="write the result here as a PingTracer-style export")
    args = parser.parse_args(argv)

    if args.mode == "reflect":
        reflector = Reflector(args.bind, args.port, args.hold_forward_ms, args.hold_back_ms, args.drop_forward_every, args.drop_back_every)
        print(f"reflecting on UDP port {reflector.port}; Ctrl+C to stop")
        try:
            reflector.serve()
        except KeyboardInterrupt:
            pass
        finally:
            reflector.sock.close()
        return 0

    result = send(args.host, args.port, args.count, args.rate, args.size, args.timeout, args.clock_offset_ms, progress=True)
    print(summary(result))
    if args.csv:
        write_csv(result, args.csv)
        print(f"\nwrote {args.csv}")
    return 0 if result.answered else 1


if __name__ == "__main__":
    sys.exit(main())
