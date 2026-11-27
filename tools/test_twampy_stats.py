#!/usr/bin/env python3
"""Checks twampy_stats.py.

    py -3.15 test_twampy_stats.py

First on a made-up twampy log with known contents, so the reading of the log and the placing of
losses can be checked exactly.  Then, if twampy has been downloaded, on a real run of twampy over
udp_delay_relay.py with known delays and losses.  It also runs under pytest.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import twampy_stats
from ping_export import read_export, summarise
from twampy_stats import ANSWERED, LOST_BACK, LOST_OUT, LOST_UNKNOWN


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)

    def equal(self, name: str, got, want) -> None:
        self.check(name, got == want, f"{got}" + ("" if got == want else f"   (expected {want})"))

    def near(self, name: str, got: float, want: float, tolerance: float) -> None:
        self.check(name, abs(got - want) <= tolerance, f"measured {got:.2f}, set {want:g}, allowed +/- {tolerance:g}")


def made_up_log() -> list[str]:
    """Twelve probes 100 ms apart, written the way twampy logs them.

    Probe 3 never reaches the reflector.  Probes 6 and 7 reach it but their replies are lost.
    Probe 9 never reaches it.  Probe 11 is sent and nothing more is heard.
    The reflector therefore numbers what it receives 0,1,2,(3),3,4,[5],[6],7,(9),8 and so on.
    """
    lines = []
    reflector = 0
    for i in range(12):
        stamp = f"26/11/27 10:00:{i // 10:02d},{(i % 10) * 100}"
        lines.append(f"{stamp:<22} INFO     Sent to 192.0.2.7 [sseq={i}]")
        if i in (3, 9, 11):
            continue                      # never reached the reflector (11: unknown to the sender)
        if i in (6, 7):
            reflector += 1                # reached it; reply lost
            continue
        out, back = 30.0 + i, 80.0 + 2 * i
        reply_ms = (i % 10) * 100 + int(out + back)
        stamp = f"26/11/27 10:00:{i // 10 + reply_ms // 1000:02d},{reply_ms % 1000}"
        lines.append(f"{stamp:<22} INFO     Reply from 192.0.2.7 [rseq={reflector} sseq={i} rtt={out + back:.2f}ms outbound={out:.2f}ms inbound={back:.2f}ms]")
        reflector += 1
    lines.append("===============================================================================")
    lines.append("  Outbound:       30.00ms     40.00ms     34.00ms       1.0ms     16.7%")
    return lines


def run(checks: Checks) -> None:
    print(" reading a made-up twampy log")
    probes, far_end = twampy_stats.parse_log(made_up_log())
    checks.equal("far end", far_end, "192.0.2.7")
    checks.equal("probes found", len(probes), 12)
    checks.equal("what happened to each probe", [p.fate for p in probes],
                 [ANSWERED, ANSWERED, ANSWERED, LOST_OUT, ANSWERED, ANSWERED, LOST_BACK, LOST_BACK, ANSWERED, LOST_OUT, ANSWERED, LOST_UNKNOWN])
    checks.equal("probe 4: out, back, round trip", (probes[4].outbound_ms, probes[4].inbound_ms, probes[4].round_trip_ms), (34.0, 88.0, 122.0))
    checks.equal("send times are 100 ms apart", [(b.sent - a.sent).total_seconds() for a, b in zip(probes, probes[1:])], [0.1] * 11)

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "made-up.csv"
        twampy_stats.write_csv(probes, far_end, path)
        hosts = read_export(path)
        checks.equal("three directions written", list(hosts), ["192.0.2.7 outbound", "192.0.2.7 inbound", "192.0.2.7 round trip"])
        outbound, inbound, both = (summarise(name, pings) for name, pings in hosts.items())
        # outbound: 7 answered + 2 lost on the way out.  inbound: 7 answered + 2 lost on the way back.  round trip: all 12.
        checks.equal("outbound: probes, lost", (outbound.pings, outbound.lost), (9, 2))
        checks.equal("inbound: probes, lost", (inbound.pings, inbound.lost), (9, 2))
        checks.equal("round trip: probes, lost", (both.pings, both.lost), (12, 5))
        checks.equal("outbound baseline, max", (outbound.baseline, outbound.maximum), (30.0, 40.0))
        checks.equal("inbound baseline, max", (inbound.baseline, inbound.maximum), (80.0, 100.0))
        checks.equal("inbound: one outage, of the two lost in a row", [(o.lost, o.ongoing) for o in inbound.outages], [(2, False)])
        text = twampy_stats.statistics(probes, far_end, path)
        import re
        found = {kind: int(re.search(re.escape(kind) + r"\s+(\d+)\s+\(", text).group(1)) for kind in (LOST_OUT, LOST_BACK, LOST_UNKNOWN)}
        checks.equal("report counts each kind of loss", found, {LOST_OUT: 2, LOST_BACK: 2, LOST_UNKNOWN: 1})
        checks.check("no clock warning when nothing reads zero", "exactly 0" not in text)

    print(" a log where one direction is stuck at zero")
    clamped = [line.replace("inbound=", "inbound=0.00ms was=") if "Reply" in line else line for line in made_up_log()]
    clamped = [line.split(" was=")[0] + "]" if " was=" in line else line for line in clamped]
    zero_probes, _ = twampy_stats.parse_log(clamped)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "clamped.csv"
        twampy_stats.write_csv(zero_probes, "192.0.2.7", path)
        checks.check("warns that the clocks disagree", "inbound delays are exactly 0" in twampy_stats.statistics(zero_probes, "192.0.2.7", path))

    print(" a real twampy run over the delaying relay")
    src = twampy_stats.DEFAULT_TWAMPY_SRC
    if not (src / "twampy" / "__main__.py").exists():
        print(f"  twampy is not in {src}: skipped")
        return
    from udp_delay_relay import Relay
    responder = subprocess.Popen([sys.executable, "-I", "-c", twampy_stats.BOOTSTRAP, str(src), "responder", "127.0.0.1:20011", "-q"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    relay = Relay(20012, ("127.0.0.1", 20011), out_ms=30, back_ms=80, out_loss=0.1, back_loss=0.1, seed=3).start()
    try:
        lines = twampy_stats.run_twampy("127.0.0.1:20012", "127.0.0.1:20010", 150, 100, src, echo=False)
    finally:
        relay.stop()
        responder.kill()
    real, far_end = twampy_stats.parse_log(lines)
    counts = {fate: sum(1 for p in real if p.fate == fate) for fate in (ANSWERED, LOST_OUT, LOST_BACK, LOST_UNKNOWN)}
    checks.equal("every probe twampy sent was found in its log", len(real), 150)
    # the relay's own count is the truth; a loss after the last reply cannot be placed, so allow for those
    unknown = counts[LOST_UNKNOWN]
    checks.check("losses on the way out match the relay", 0 <= relay.counts["out_lost"] - counts[LOST_OUT] <= unknown,
                 f"script {counts[LOST_OUT]}, relay {relay.counts['out_lost']}, unplaced {unknown}")
    checks.check("losses on the way back match the relay", 0 <= relay.counts["back_lost"] - counts[LOST_BACK] <= unknown,
                 f"script {counts[LOST_BACK]}, relay {relay.counts['back_lost']}, unplaced {unknown}")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "real.csv"
        twampy_stats.write_csv(real, far_end, path)
        outbound, inbound, both = (summarise(name, pings) for name, pings in read_export(path).items())
        checks.near("outbound median", outbound.median, 30, 6)
        checks.near("inbound median", inbound.median, 80, 6)
        checks.near("round trip median", both.median, 110, 10)
        # twampy's own table, as a cross-check on the averages
        table = {line.split(":")[0].strip(): line.split() for line in lines if line.strip().startswith(("Outbound:", "Inbound:", "Roundtrip:"))}
        if "Outbound:" in "".join(lines):
            twampy_out_avg = float(table["Outbound"][3].rstrip("ms"))
            checks.near(f"outbound mean agrees with twampy's own table ({twampy_out_avg} ms)", outbound.mean, twampy_out_avg, 0.05)
            twampy_in_avg = float(table["Inbound"][3].rstrip("ms"))
            checks.near(f"inbound mean agrees with twampy's own table ({twampy_in_avg} ms)", inbound.mean, twampy_in_avg, 0.05)


def test_twampy_stats() -> None:
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
