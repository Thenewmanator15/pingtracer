#!/usr/bin/env python3
"""Checks voip_score.py against answers worked out by hand, then, if irtt has been built,
on a real call-shaped irtt run through the delay relay.

    py -3.15 test_voip_score.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

import irtt_stats
import voip_score as voip
from twampy_stats import ANSWERED, LOST_BACK, LOST_OUT
from udp_delay_relay import Relay

failures = 0


def check(name: str, passed: bool, detail: str) -> None:
    global failures
    failures += not passed
    print(f"  {'ok  ' if passed else 'FAIL'} {name}: {detail}")


def near(a: float, b: float, tolerance: float = 1e-3) -> bool:
    return abs(a - b) <= tolerance


def test_formulas() -> None:
    print("1. Formulas, against hand-worked figures")
    check("delay points at 100 ms", near(voip.delay_points(100), 2.4), f"{voip.delay_points(100):.3f} (0.024 x 100 = 2.400)")
    check("delay points at 300 ms", near(voip.delay_points(300), 20.697), f"{voip.delay_points(300):.3f} (7.2 + 0.11 x 122.7 = 20.697)")
    got = voip.missing_points(2.0, 1.0, 0.0, 4.3)
    check("2% scattered loss, G.711", near(got, 190 / 6.3), f"{got:.3f} (95 x 2 / (2 + 4.3) = 30.159)")
    got = voip.missing_points(2.0, 2.0, 0.0, 4.3)
    check("2% clumped loss costs more", near(got, 190 / 5.3), f"{got:.3f} (95 x 2 / (2/2 + 4.3) = 35.849)")
    check("no loss costs only the codec's own", voip.missing_points(0, 1, 11, 19) == 11, "Ie 11 -> 11")
    check("best score maps to MOS 4.41", near(voip.mos_from_r(93.2), 4.41, 0.005), f"R 93.2 -> MOS {voip.mos_from_r(93.2):.3f}")
    check("R 50 maps to MOS 2.58", near(voip.mos_from_r(50), 2.575, 0.001), f"R 50 -> MOS {voip.mos_from_r(50):.3f} (1 + 1.75 - 0.175)")
    check("MOS stays within 1 to 4.5", voip.mos_from_r(-5) == 1.0 and voip.mos_from_r(120) == 4.5, "R -5 -> 1, R 120 -> 4.5")
    check("bands", (voip.band(91), voip.band(85), voip.band(49)) == ("very satisfied", "satisfied", "not recommended"), "91, 85, 49")


def test_bursts() -> None:
    print("2. Clumping of missing packets")
    pattern = np.tile([False] * 8 + [True] * 2, 200)             # runs of 2 missing; chance would give runs of 1.25
    check("runs of two in 20% loss", near(voip.burst_ratio(pattern), 1.6, 0.01), f"BurstR {voip.burst_ratio(pattern):.3f} (2 / 1.25 = 1.600)")
    ratios = [voip.burst_ratio(np.random.default_rng(seed).random(20000) < 0.03) for seed in range(20)]
    check("scattered loss reads about 1", max(ratios) < 1.08, f"largest of 20 records {max(ratios):.3f}")
    check("no loss reads 1", voip.burst_ratio(np.zeros(100, dtype=bool)) == 1.0, "1.0")


def test_buffer() -> None:
    print("3. The buffer")
    delays = np.full(1000, 50.0)
    delays[::100] += 80.0                                        # 10 packets arrive 80 ms later than the rest
    fixed = voip.score_leg("x", delays, one_way_ms=50.0, buffer_ms=40.0)
    expected = 93.2 - 0.024 * 110 - 95 * 1.0 / (1.0 + 4.3)
    check("a 40 ms buffer throws the 10 away", fixed.late == 10 and near(fixed.r, expected),
          f"{fixed.late} too late, R {fixed.r:.3f} (93.2 - 2.64 - 17.925 = {expected:.3f})")
    best = voip.score_leg("x", delays, one_way_ms=50.0)
    check("the best buffer keeps them", best.buffer_ms == 80.0 and best.late == 0 and near(best.r, 93.2 - 0.024 * 150),
          f"buffer {best.buffer_ms:.0f} ms, R {best.r:.3f} (93.2 - 3.6 = 89.600)")
    lossy = delays.copy()
    lossy[5::50] = np.nan                                        # 20 never arrive
    leg = voip.score_leg("x", lossy, one_way_ms=50.0, buffer_ms=100.0)
    check("lost packets are counted", (leg.lost, leg.late) == (20, 0) and near(leg.missing_percent, 2.0), f"{leg.lost} lost, {leg.missing_percent:.2f}%")
    shifted = voip.score_leg("x", delays - 500.0, one_way_ms=50.0)      # a far clock half a second out
    check("a clock offset changes nothing", near(shifted.r, best.r) and shifted.buffer_ms == best.buffer_ms, f"R {shifted.r:.3f} against {best.r:.3f}")


def test_real_call_over_relay() -> bool:
    """A real call-shaped irtt run with the relay between client and server.  Returns False if irtt is not there."""
    irtt = irtt_stats.DEFAULT_IRTT
    if not irtt.exists():
        return False
    print("4. A real call-shaped irtt run over the relay (30 ms out, 80 ms back, 2% and 4% loss)")
    # Not "-b 127.0.0.1:20051": tied to the loopback address on Windows, irtt's server sizes its buffer from that
    # interface's packet limit, which Windows gives as 4294967295, ends up with room for a bare probe only,
    # and stops at the first call-sized packet (seen here).  Left on all addresses it works.
    server = subprocess.Popen([str(irtt), "server", "-b", ":20051"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    relay = Relay(20052, ("127.0.0.1", 20051), out_ms=30, back_ms=80, out_loss=0.02, back_loss=0.04, seed=5).start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            result = irtt_stats.run_irtt("127.0.0.1:20052", 20, voip.PACKET_MS, irtt, Path(folder) / "call.json", voip.PACKET_BYTES)
    finally:
        time.sleep(0.3)
        relay.stop()
        server.kill()
    params = result["config"]["params"]
    check("irtt sent a call-shaped stream", params["interval"] == 20_000_000 and params["length"] == voip.PACKET_BYTES,
          f"a {params['length']}-byte packet every {params['interval'] / 1e6:g} ms")
    probes, _ = irtt_stats.read_result(result)
    fates = [p.fate for p in probes]
    out, back = voip.score_call(probes)
    check("packet count", 990 <= len(probes) <= 1001, f"{len(probes)} packets in 20 s")
    check("losses match the relay's own count", abs(out.lost - relay.counts["out_lost"]) <= 2 and abs(back.lost - relay.counts["back_lost"]) <= 2,
          f"out {out.lost} (relay {relay.counts['out_lost']}), back {back.lost} (relay {relay.counts['back_lost']})")
    check("packets are counted in the directions they travelled", out.packets == fates.count(ANSWERED) + fates.count(LOST_OUT) + fates.count(LOST_BACK)
          and back.packets == fates.count(ANSWERED) + fates.count(LOST_BACK), f"{out.packets} went out, {back.packets} set off back")
    check("one-way delay is half the fastest round trip", 55 <= out.one_way_ms <= 58 and out.one_way_ms == back.one_way_ms,
          f"{out.one_way_ms:.2f} ms (set 30 + 80 = 110 round trip)")
    trusted_out, trusted_back = voip.score_call(probes, trust_clocks=True)
    check("with one clock, each direction's own delay", 30 <= trusted_out.one_way_ms <= 33 and 80 <= trusted_back.one_way_ms <= 83,
          f"{trusted_out.one_way_ms:.2f} and {trusted_back.one_way_ms:.2f} ms (set 30 and 80)")
    check("more loss scores lower", back.r < out.r < 93.2, f"R {out.r:.1f} out, {back.r:.1f} back")
    print(f"     out: R {out.r:.1f} MOS {out.mos:.2f}, buffer {out.buffer_ms:.0f} ms, {out.missing_percent:.2f}% missing, BurstR {out.burst_r:.2f}")
    print(f"     back: R {back.r:.1f} MOS {back.mos:.2f}, buffer {back.buffer_ms:.0f} ms, {back.missing_percent:.2f}% missing, BurstR {back.burst_r:.2f}")
    return True


def main() -> int:
    for test in (test_formulas, test_bursts, test_buffer):
        test()
    if not test_real_call_over_relay():
        print(f"4. SKIP real run: {irtt_stats.DEFAULT_IRTT} not found")
    print("\nTESTS PASSED" if not failures else f"\n{failures} CHECK(S) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
