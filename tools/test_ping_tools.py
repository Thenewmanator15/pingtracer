#!/usr/bin/env python3
"""Checks ping_export.py, ping_report.py and ping_histogram.py.

    py -3.15 test_ping_tools.py

It writes an export with known contents, reads it back, and compares every
figure with a value worked out a second way.  When NumPy is installed the
percentiles, mean, jitter and histogram counts are also computed with NumPy
and shown side by side.  It also runs under pytest.
"""

from __future__ import annotations

import csv
import math
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from ping_export import TIME_FORMATS, Ping, choose_times, find_outages, histogram, jitter, percentile, read_export, summarise
from ping_report import report, text_histogram

SAMPLE_HOST = "sample [192.0.2.1]"
OLD_FORMAT_HOST = 'steady, "quoted" [192.0.2.2]'


def write_sample(path: Path) -> None:
    """Writes an export with known contents: 500 pings a second apart.

    One lost ping alone at 50, five lost from 100, three lost at the very end,
    and one reply at 200 whose fourth byte came back changed.
    """
    start = datetime(2026, 11, 26, 21, 0, 0)
    stamp = TIME_FORMATS[0]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Host", "Sent", "Received", "Result", "RoundTripMs", "MeasuredMs", "PayloadBytes", "SentData", "ReceivedData"])
        for i in range(500):
            sent = start + timedelta(seconds=i)
            lost = i == 50 or 100 <= i <= 104 or i >= 497
            rtt = 20 + (i * 7) % 5 + (30 if i % 37 == 0 else 0) + (280 if i in (150, 300, 301) else 0)
            data = "55aa55aa55aa55aa"
            received = (sent + timedelta(milliseconds=rtt + 1.5)).strftime(stamp)
            if lost:
                writer.writerow([SAMPLE_HOST, sent.strftime(stamp), "", "TimedOut", "", "", 8, data, ""])
            elif i == 200:
                writer.writerow([SAMPLE_HOST, sent.strftime(stamp), received, "DataMismatch", rtt, f"{rtt + 1.5:.3f}", 8, data, "55aa55ab55aa55aa"])
            else:
                writer.writerow([SAMPLE_HOST, sent.strftime(stamp), received, "Success", rtt, f"{rtt + 1.5:.3f}", 8, data, data])
        # an older export leaves the newer columns empty; a second host exercises that path
        for i in range(60):
            writer.writerow([OLD_FORMAT_HOST, (start + timedelta(seconds=i)).strftime(stamp), "", "Success", 5, "", "", "", ""])


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, got, want) -> None:
        close = isinstance(got, float) and isinstance(want, (int, float)) and math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9)
        ok = got == want or close
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got}" + ("" if ok else f"   (expected {want})"))
        if not ok:
            self.failures.append(name)


def run(checks: Checks) -> None:
    check = checks.check
    with tempfile.TemporaryDirectory() as folder:
        sample = Path(folder) / "sample.csv"
        write_sample(sample)
        hosts = read_export(sample)
        check("hosts read", sorted(hosts), sorted([SAMPLE_HOST, OLD_FORMAT_HOST]))
        pings = hosts[SAMPLE_HOST]
        s = summarise(SAMPLE_HOST, pings)

        print(" counts and events")
        check("pings", s.pings, 500)
        check("lost (1 single + 5 + 3 at the end)", s.lost, 9)
        check("loss percent", s.loss_percent, 1.8)
        check("outages (the single lost ping is not one)", len(s.outages), 2)
        check("first outage start", s.outages[0].start, datetime(2026, 11, 26, 21, 1, 40))
        check("first outage length in seconds", s.outages[0].seconds, 5.0)
        check("first outage pings lost", s.outages[0].lost, 5)
        check("last outage still down", s.outages[1].ongoing, True)
        check("outages with threshold 1", len(find_outages(pings, 1)), 3)
        check("altered replies", len(s.mismatches), 1)
        check("altered byte offset", s.mismatches[0].differing_offsets, [3])

        print(" latency, against values worked out a second way")
        values = [p.rtt_ms for p in pings if p.replied]
        ordered = sorted(values)
        check("baseline", s.baseline, 20)
        check("max", s.maximum, ordered[-1])
        check("mean", s.mean, sum(values) / len(values))
        check("worst 1% (rank ceil(0.99 n))", s.p99, ordered[math.ceil(0.99 * len(ordered)) - 1])
        check("worst 1% with 50 values is the max", percentile(list(range(50)), 0.99), 49)
        check("worst 1% of 99 x 20 and one 300", percentile([20] * 99 + [300], 0.99), 20)
        check("worst 1% of 98 x 20, 150, 300", percentile([20] * 98 + [150, 300], 0.99), 150)
        check("jitter of 20,20,220,20", jitter([Ping(datetime.min, "Success")] * 4, [20, 20, 220, 20]), 400 / 3)
        broken = [Ping(datetime.min, "Success"), Ping(datetime.min, "TimedOut"), Ping(datetime.min, "Success"), Ping(datetime.min, "Success")]
        check("jitter: a loss breaks the pair", jitter(broken, [20, None, 500, 510]), 10.0)
        check("histogram bins", histogram([20, 20, 21, 23]), ([20.0, 21.0, 22.0, 23.0], [2, 1, 0, 1], 1.0))
        check("histogram counts add up", sum(histogram(values)[1]), len(values))
        fine = [30.0, 30.05, 30.1, 30.1, 30.34, 30.9]
        check("finer figures get a finer bin", histogram(fine)[2], 0.02)
        check("finer figures: counts add up, edge values in their own bin", (sum(histogram(fine, 0.1)[1]), histogram(fine, 0.1)[1][:4]), (6, [2, 2, 0, 1]))
        check("whole numbers still use 1 ms", histogram([20, 21, 25])[2], 1.0)

        try:
            import numpy as np
        except ImportError:
            print(" NumPy not installed: skipped the side-by-side comparison")
        else:
            print(" side by side with NumPy")
            arr = np.array(values, dtype=float)
            for name, mine, theirs in (
                ("median", s.median, float(np.percentile(arr, 50, method="inverted_cdf"))),
                ("95%", s.p95, float(np.percentile(arr, 95, method="inverted_cdf"))),
                ("worst 1%", s.p99, float(np.percentile(arr, 99, method="inverted_cdf"))),
                ("mean", s.mean, float(arr.mean())),
            ):
                check(f"{name}: these scripts {mine:g}, NumPy {theirs:g}", mine, theirs)
            # jitter by NumPy: differences between neighbours, keeping only pairs where neither ping was lost
            series = np.array([p.rtt_ms if p.replied else np.nan for p in pings], dtype=float)
            numpy_jitter = float(np.nanmean(np.abs(np.diff(series))))
            check(f"jitter: these scripts {s.jitter:g}, NumPy {numpy_jitter:g}", s.jitter, numpy_jitter)
            numpy_counts, _ = np.histogram(arr, bins=np.arange(arr.min(), arr.max() + 2))
            check("histogram counts equal NumPy's", histogram(values, 1.0)[1] == [int(c) for c in numpy_counts], True)

        print(" older export format")
        steady = summarise(OLD_FORMAT_HOST, hosts[OLD_FORMAT_HOST])
        check("old-format host: pings, loss, jitter", (steady.pings, steady.lost, steady.jitter), (60, 0, 0.0))

        print(" report script")
        text = report(s)
        check("report names both outages", text.count("pings lost"), 2)
        check("report shows the altered byte", "bytes 3" in text, True)
        check("text histogram has a line per non-empty bin", len(text_histogram([20, 20, 21, 23], None)), 3)

        print(" spike script")
        import ping_spikes
        t0 = datetime(2026, 11, 26, 21, 0, 0)

        def series(times: list[float | None]) -> list[Ping]:
            return [Ping(t0 + timedelta(seconds=i), "Success" if t is not None else "TimedOut", rtt_ms=t) for i, t in enumerate(times)]

        near = series([2, 2, 3, 2, 60, 70, 2, 2, 3, 2, 2, 2, 40, 2, 2, 2])       # spikes at 4-5 and at 12
        far = series([20, 21, 20, 20, 95, 99, 20, 20, 90, 20, 21, 20, 20, 20, 20, 20])  # spikes at 4-5 and at 8
        check("threshold: median + 10 when the spread is small", ping_spikes.spike_threshold([2, 2, 3, 2, 60, 70, 2, 2]), (2, 12))
        check("threshold: median + 3x spread when the host is noisy", ping_spikes.spike_threshold([10, 20, 30, 40, 50, 60, 70, 80]), (40, 160))
        check("threshold: --above-ms overrides", ping_spikes.spike_threshold([2, 2, 3, 2], above_ms=25), (2, 27))
        found = ping_spikes.analyse({"near": near, "far": far})
        near_spikes, far_spikes = found[0].spikes, found[1].spikes
        check("near host: spikes found (two pings in a row are one)", [(s.start.second, s.pings, s.peak_ms) for s in near_spikes], [(4, 2, 70), (12, 1, 40)])
        check("far host: spikes found", [(s.start.second, s.pings, s.peak_ms) for s in far_spikes], [(4, 2, 99), (8, 1, 90)])
        check("spike at 4 s is shared, the others are not", [bool(s.shared_with) for s in near_spikes + far_spikes], [True, False, True, False])
        check("a lost ping is not a spike", len(ping_spikes.find_spikes("x", series([2, None, 2, 2]), [2, None, 2, 2]).spikes), 0)
        evenly = [ping_spikes.Spike("x", t0 + timedelta(seconds=30 * i), t0 + timedelta(seconds=30 * i), 1, 50) for i in range(6)]
        check("evenly spaced spikes: every 30 s, no variation", ping_spikes.regularity(evenly), (30.0, 0.0))
        check("too few spikes to judge spacing", ping_spikes.regularity(evenly[:3]), None)
        sample_spikes = ping_spikes.find_spikes(SAMPLE_HOST, pings, choose_times(pings))
        check("sample file: threshold", sample_spikes.threshold_ms, 32.0)
        check("sample file: spikes (14 every 37 s, one at 150 s, one two-ping at 300 s)", len(sample_spikes.spikes), 16)
        check("spike report mentions the shared moment", "also: far" in ping_spikes.report(found), True)

        print(" numbered moments and measured figures")
        moments = ping_spikes.list_moments(found)
        check("moments in time order: M1 shared at 4 s, M2 far at 8 s, M3 near at 12 s",
              [(m.number, m.start.second, m.hosts) for m in moments], [(1, 4, ["near", "far"]), (2, 8, ["far"]), (3, 12, ["near"])])
        check("each spike carries its moment number", ([s.moment for s in near_spikes], [s.moment for s in far_spikes]), ([1, 3], [1, 2]))
        check("peak above the host's own median", [s.above_ms for s in near_spikes], [68, 38])
        check("chance of at least 1 in 2 tries at 0.2", ping_spikes.chance_of_at_least(1, 2, 0.2), 0.36)
        check("chance of at least 2 in 2 tries at 0.5", ping_spikes.chance_of_at_least(2, 2, 0.5), 0.25)
        check("chance of at least 0 is certain", ping_spikes.chance_of_at_least(0, 5, 0.3), 1.0)
        relation = ping_spikes.relate({"near": near, "far": far}, found)[0]
        check("far host: 1 of its 2 spikes was together with the near host", (relation.together, relation.spikes), (1, 2))
        check("expected by chance (near host spiking for 3 s of a 15 s run)", relation.expected_by_chance, 0.4)
        check("probability of 1 or more by chance", relation.chance_probability, 0.36)
        check("every round paired", relation.rounds_paired, 16)
        # by hand: far is above its median by 1, 75, 79, 70, 1 (total 226); matched on near in the same round by 0, 58, 68, 1, 0 (total 127)
        check("shared delay, worked out by hand", relation.shared_delay_percent, 100 * 127 / 226)
        offset = [Ping(p.sent + timedelta(milliseconds=3), p.result, rtt_ms=p.rtt_ms) for p in far]
        check("rounds still pair when the second host is sent 3 ms later", len(ping_spikes.pair_rounds(near, [p.rtt_ms for p in near], offset, [p.rtt_ms for p in offset])), 16)
        check("report gives the figures", "shared delay      56%" in ping_spikes.report(found, [relation]), True)
        try:
            import numpy as np
        except ImportError:
            print("  NumPy not installed: skipped the correlation comparison")
        else:
            numpy_r = float(np.corrcoef([p.rtt_ms for p in near], [p.rtt_ms for p in far])[0, 1])
            check(f"correlation: these scripts {relation.correlation:.6f}, NumPy {numpy_r:.6f}", relation.correlation, numpy_r)
            a = np.array([p.rtt_ms for p in near], dtype=float); b = np.array([p.rtt_ms for p in far], dtype=float)
            numpy_shared = float(100 * np.minimum(np.clip(a - 2, 0, None), np.clip(b - 20, 0, None)).sum() / np.clip(b - 20, 0, None).sum())
            check(f"shared delay: these scripts {relation.shared_delay_percent:.4f}, NumPy {numpy_shared:.4f}", relation.shared_delay_percent, numpy_shared)

        print(" histogram script")
        try:
            import matplotlib
            matplotlib.use("Agg")  # drawing to files only; no window needed
        except ImportError:
            print("  matplotlib not installed: skipped drawing")
        else:
            from ping_histogram import plot_host, safe_name
            picture = Path(folder) / "sample.png"
            check("picture drawn", plot_host(s, pings, choose_times(pings), picture), True)
            check("picture file has content", picture.stat().st_size > 10000, True)
            check("no picture when nothing replied", plot_host(s, pings[:1], [None], Path(folder) / "none.png"), False)
            check("host name made safe for a file name", safe_name('a "b" [1.2.3.4]'), "a__b___1.2.3.4")
            spike_picture = Path(folder) / "spikes.png"
            check("spike picture drawn", ping_spikes.plot_spikes({"near": near, "far": far}, found, spike_picture), True)
            check("spike picture file has content", spike_picture.stat().st_size > 10000, True)


def test_ping_tools() -> None:
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
