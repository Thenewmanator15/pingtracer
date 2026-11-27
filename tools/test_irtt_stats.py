#!/usr/bin/env python3
"""Checks irtt_stats.py: first on a made-up irtt result with known contents, then,
if irtt has been built, on a real irtt run through the delay relay.

    py -3.15 test_irtt_stats.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import irtt_stats
from ping_export import percentile, read_export
from twampy_stats import ANSWERED, LOST_BACK, LOST_OUT, LOST_UNKNOWN, write_csv
from udp_delay_relay import Relay

START = 1_795_000_000_000_000_000   # nanoseconds


def trip(seqno: int, lost: str, send_ms: float = 0.0, receive_ms: float = 0.0) -> dict:
    stamps = {"client": {"send": {"wall": START + seqno * 100_000_000}}}
    if lost != "false":
        return {"seqno": seqno, "lost": lost, "timestamps": stamps, "delay": {}}
    return {"seqno": seqno, "lost": "false", "timestamps": stamps,
            "delay": {"send": int(send_ms * 1e6), "receive": int(receive_ms * 1e6), "rtt": int((send_ms + receive_ms) * 1e6)}}


def test_made_up_result() -> None:
    result = {"config": {"remote_address": "10.0.0.2:2112"},
              "round_trips": [trip(0, "false", 30, 80), trip(1, "true_up"), trip(2, "false", 31, 82),
                              trip(3, "true_down"), trip(4, "false", 32, 81), trip(5, "true")]}
    probes, far_end = irtt_stats.read_result(result)
    assert far_end == "10.0.0.2:2112"
    assert [p.fate for p in probes] == [ANSWERED, LOST_OUT, ANSWERED, LOST_BACK, ANSWERED, LOST_UNKNOWN]
    assert [p.outbound_ms for p in probes if p.fate == ANSWERED] == [30.0, 31.0, 32.0]
    assert [p.inbound_ms for p in probes if p.fate == ANSWERED] == [80.0, 82.0, 81.0]
    assert probes[2].round_trip_ms == 113.0
    assert (probes[1].sent - probes[0].sent).total_seconds() == 0.1
    assert irtt_stats.clock_note(probes) == ""

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "made-up.csv"
        write_csv(probes, far_end, path)
        hosts = read_export(path)
    out, back, both = (hosts[f"10.0.0.2:2112 {name}"] for name in ("outbound", "inbound", "round trip"))
    assert (len(out), sum(not p.replied for p in out)) == (4, 1)      # 3 answered + the one lost on the way out
    assert (len(back), sum(not p.replied for p in back)) == (4, 1)    # 3 answered + the one lost on the way back
    assert (len(both), sum(not p.replied for p in both)) == (6, 3)

    # A far clock 50 ms behind: the way out reads below zero, and the script says so.
    shifted, _ = irtt_stats.read_result({"round_trips": [trip(i, "false", -20, 130) for i in range(5)]})
    assert "way out" in irtt_stats.clock_note(shifted)


def test_real_irtt_over_relay() -> bool:
    """A real irtt client and server with the relay between them.  Returns False if irtt is not there."""
    irtt = irtt_stats.DEFAULT_IRTT
    if not irtt.exists():
        return False
    server = subprocess.Popen([str(irtt), "server", "-b", "127.0.0.1:20021"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    relay = Relay(20022, ("127.0.0.1", 20021), out_ms=30, back_ms=80, out_loss=0.1, back_loss=0.1, seed=3).start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            result = irtt_stats.run_irtt("127.0.0.1:20022", 15, 100, irtt, Path(folder) / "run.json")
    finally:
        time.sleep(0.3)
        relay.stop()
        server.kill()
    probes, _ = irtt_stats.read_result(result)
    fates = [p.fate for p in probes]
    assert len(probes) >= 140, len(probes)
    # The relay also carries irtt's opening and closing packets, which it may lose too, so its counts can
    # exceed the probes lost by those few; with none of them lost the counts are equal.
    assert 0 <= relay.counts["out_lost"] - fates.count(LOST_OUT) <= 2, (relay.counts, fates.count(LOST_OUT))
    assert 0 <= relay.counts["back_lost"] - fates.count(LOST_BACK) <= 2, (relay.counts, fates.count(LOST_BACK))
    assert fates.count(LOST_UNKNOWN) == 0
    answered = [p for p in probes if p.fate == ANSWERED]
    out, back = percentile([p.outbound_ms for p in answered], 0.5), percentile([p.inbound_ms for p in answered], 0.5)
    assert 30 <= out <= 36, out
    assert 80 <= back <= 86, back
    print(f"  real irtt run: {len(probes)} probes, lost {fates.count(LOST_OUT)} out / {fates.count(LOST_BACK)} back "
          f"(relay {relay.counts['out_lost']} / {relay.counts['back_lost']}), medians {out:.2f} / {back:.2f} ms")
    return True


def main() -> int:
    test_made_up_result()
    print("PASS made-up irtt result")
    if test_real_irtt_over_relay():
        print("PASS real irtt run over the relay")
    else:
        print(f"SKIP real irtt run: {irtt_stats.DEFAULT_IRTT} not found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
