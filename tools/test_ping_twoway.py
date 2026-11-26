#!/usr/bin/env python3
"""Checks ping_twoway.py by running both ends on this machine.

    py -3.15 test_ping_twoway.py

With both ends on one machine there is only one clock, so the true answer is
known: the reflector is told to hold probes for a set time in each direction
and to lose some in each direction, and the sender has to report those figures.
It also runs under pytest.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import ping_twoway
from ping_export import read_export, summarise

TOLERANCE_MS = 6.0   # how close a measured delay must be to the delay that was injected


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)

    def near(self, name: str, got: float, want: float, tolerance: float = TOLERANCE_MS) -> None:
        self.check(name, abs(got - want) <= tolerance, f"measured {got:.2f} ms, injected {want:g} ms, allowed +/- {tolerance:g}")


def run_case(hold_forward: float, hold_back: float, drop_forward: int = 0, drop_back: int = 0, count: int = 40, rate: float = 20.0) -> ping_twoway.Result:
    reflector = ping_twoway.Reflector("127.0.0.1", 0, hold_forward, hold_back, drop_forward, drop_back).start()
    try:
        return ping_twoway.send("127.0.0.1", reflector.port, count=count, rate=rate, timeout=1.0)
    finally:
        reflector.stop()


def run(checks: Checks) -> None:
    def medians(result: ping_twoway.Result) -> tuple[float, float, float]:
        done = [p for p in result.probes if p.answered]
        return (ping_twoway.median([result.forward_ms(p) for p in done]),
                ping_twoway.median([result.backward_ms(p) for p in done]),
                ping_twoway.median([result.round_trip_ms(p) for p in done]))

    print(" nothing injected")
    plain = run_case(0, 0)
    forward, backward, round_trip = medians(plain)
    checks.check("every probe answered", plain.answered == plain.sent == 40, f"{plain.answered} of {plain.sent}")
    checks.near("forward", forward, 0, 2)
    checks.near("backward", backward, 0, 2)
    checks.check("no loss either way", (plain.lost_forward, plain.lost_backward) == (0, 0))

    print(" 30 ms held on the way out, 80 ms on the way back")
    held = run_case(30, 80)
    forward, backward, round_trip = medians(held)
    checks.near("forward", forward, 30)
    checks.near("backward", backward, 80)
    checks.near("round trip", round_trip, 110, 2 * TOLERANCE_MS)
    checks.check("backward reads longer than forward by about 50 ms", 40 <= backward - forward <= 60, f"{backward - forward:.2f} ms")
    done = [p for p in held.probes if p.answered]
    worst = max(abs((held.forward_ms(p) + held.backward_ms(p) + held.held_ms(p)) - (p.t4 - p.t1) / 1e6) for p in done)
    checks.check("forward + held + backward equals the total for every probe", worst < 1e-6, f"largest difference {worst:.2e} ms")

    print(" the other way round: 80 ms out, 30 ms back")
    swapped = run_case(80, 30)
    forward, backward, _ = medians(swapped)
    checks.near("forward", forward, 80)
    checks.near("backward", backward, 30)

    print(" every 4th probe lost on the way out")
    out_loss = run_case(0, 0, drop_forward=4)
    checks.check("10 of 40 lost on the way out, none on the way back", (out_loss.lost_forward, out_loss.lost_backward) == (10, 0),
                 f"out {out_loss.lost_forward}, back {out_loss.lost_backward}, answered {out_loss.answered}")

    print(" every 5th reply lost on the way back")
    back_loss = run_case(0, 0, drop_back=5)
    checks.check("none lost on the way out, 8 of 40 on the way back", (back_loss.lost_forward, back_loss.lost_backward) == (0, 8),
                 f"out {back_loss.lost_forward}, back {back_loss.lost_backward}, answered {back_loss.answered}")

    checks.check("the reflector confirmed its final count", back_loss.count_confirmed)

    print(" both at once: every 4th lost out, every 5th lost back")
    both = run_case(0, 0, drop_forward=4, drop_back=5)
    # 10 never arrive (4, 8, ... 40).  Of the 30 that do, those numbered 5, 10, 15, 25, 30, 35 lose their reply: 6.
    checks.check("10 lost on the way out, 6 on the way back", (both.lost_forward, both.lost_backward) == (10, 6),
                 f"out {both.lost_forward}, back {both.lost_backward}, answered {both.answered}")

    print(" a stated clock difference is taken off")
    forward_raw = ping_twoway.median([held.forward_ms(p) for p in done])
    corrected = ping_twoway.Result(held.host, held.probes, held.reflector_received, clock_offset_ms=10.0)
    checks.near("forward falls by 10 ms", ping_twoway.median([corrected.forward_ms(p) for p in done]), forward_raw - 10, 0.001)
    checks.near("backward rises by 10 ms", ping_twoway.median([corrected.backward_ms(p) for p in done]), backward_of(held, done) + 10, 0.001)
    checks.near("round trip does not change", ping_twoway.median([corrected.round_trip_ms(p) for p in done]), medians(held)[2], 0.001)
    checks.near("symmetric-path clock estimate is half the injected difference (30 vs 80 -> -25)", held.symmetric_clock_offset_ms(), -25, TOLERANCE_MS)

    print(" nothing listening")
    silent = ping_twoway.send("127.0.0.1", 9, count=5, rate=50, timeout=0.3)
    checks.check("no replies, and all counted as lost on the way out", (silent.answered, silent.lost_forward) == (0, 5))

    print(" the CSV works with the other scripts")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "twoway.csv"
        ping_twoway.write_csv(held, str(path))
        hosts = read_export(path)
        checks.check("three directions written as three hosts", list(hosts) == ["127.0.0.1 forward", "127.0.0.1 backward", "127.0.0.1 round trip"])
        by_name = {name.split(" ", 1)[1]: summarise(name, pings) for name, pings in hosts.items()}
        checks.near("ping_export median, forward", by_name["forward"].median, 30)
        checks.near("ping_export median, backward", by_name["backward"].median, 80)
        ping_twoway.write_csv(out_loss, str(path))
        lossy = summarise("x", read_export(path)["127.0.0.1 forward"])
        checks.check("lost probes appear as lost in the export", (lossy.pings, lossy.lost) == (40, 10), f"{lossy.lost} of {lossy.pings}")


def backward_of(result: ping_twoway.Result, done: list) -> float:
    return ping_twoway.median([result.backward_ms(p) for p in done])


def test_ping_twoway() -> None:
    """Entry point for pytest."""
    checks = Checks()
    run(checks)
    assert not checks.failures, checks.failures


def main() -> int:
    checks = Checks()
    run(checks)
    print()
    print("TESTS " + ("FAILED: " + ", ".join(checks.failures) if checks.failures else "PASSED"))
    return 1 if checks.failures else 0


if __name__ == "__main__":
    sys.exit(main())
