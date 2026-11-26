#!/usr/bin/env python3
"""Analyse a PingTracer CSV export (Export > Ping data to CSV).

For every host in the file this prints latency statistics, lists drop-outs and
any replies whose data came back altered, and draws a histogram of the ping
times with a strip of the pings over time underneath.

    py -3.13 ping_analysis.py "PingTracer 1.1.1.1 2026-11-26.csv"
    py -3.13 ping_analysis.py export.csv --out plots --min-outage 3
    py -3.13 ping_analysis.py --self-test

The statistics need only the standard library.  The pictures need matplotlib;
without it everything else still works and a text histogram is printed.

The numbers are worked out the same way PingTracer works out its own, so the
two can be compared:

* baseline  = the shortest ping time (the path with nothing queued)
* worst 1%  = the smallest time that at least 99% of pings were at or below
* jitter    = the mean change between one ping and the next; a lost ping
              breaks the pairing
* loss      = pings with a result that was not a success; pings still waiting
              for a reply are never written to the export

PingTracer's on-screen figures cover only the pings visible in its window.
This covers every ping in the file.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")
SUCCESS_RESULTS = ("Success", "DataMismatch")


# --------------------------------------------------------------------------- data


@dataclass
class Ping:
    sent: datetime
    result: str
    rtt_ms: float | None = None        # the operating system's figure, whole ms
    measured_ms: float | None = None   # the app's own Received - Sent
    received: datetime | None = None
    payload_bytes: int | None = None
    sent_data: str = ""
    received_data: str = ""

    @property
    def replied(self) -> bool:
        return self.result in SUCCESS_RESULTS


@dataclass
class Outage:
    start: datetime
    end: datetime
    lost: int
    ongoing: bool = False

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class Mismatch:
    sent: datetime
    sent_bytes: int
    received_bytes: int
    differing_offsets: list[int]


@dataclass
class Summary:
    host: str
    pings: int = 0
    replies: int = 0
    lost: int = 0
    first: datetime | None = None
    last: datetime | None = None
    baseline: float = math.nan
    median: float = math.nan
    mean: float = math.nan
    p95: float = math.nan
    p99: float = math.nan
    maximum: float = math.nan
    jitter: float = math.nan
    queueing_mean: float = math.nan
    outages: list[Outage] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    failure_kinds: dict[str, int] = field(default_factory=dict)

    @property
    def loss_percent(self) -> float:
        return 100.0 * self.lost / self.pings if self.pings else math.nan


def parse_time(text: str) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError(f"unrecognised time: {text!r}")


def parse_number(text: str) -> float | None:
    text = (text or "").strip()
    return float(text) if text else None


def read_export(path: str | Path) -> dict[str, list[Ping]]:
    """Reads an export into {host: [pings in file order]}.

    Older exports without the Received, MeasuredMs or data columns are accepted;
    the missing values are simply left empty.
    """
    hosts: dict[str, list[Ping]] = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "Host" not in reader.fieldnames:
            raise ValueError(f"{path}: not a PingTracer export (no Host column)")
        time_column = "Sent" if "Sent" in reader.fieldnames else "Time"
        for row in reader:
            size = parse_number(row.get("PayloadBytes", ""))
            ping = Ping(
                sent=parse_time(row[time_column]),
                result=(row.get("Result") or "").strip(),
                rtt_ms=parse_number(row.get("RoundTripMs", "")),
                measured_ms=parse_number(row.get("MeasuredMs", "")),
                received=parse_time(row.get("Received", "")),
                payload_bytes=int(size) if size is not None else None,
                sent_data=(row.get("SentData") or "").strip(),
                received_data=(row.get("ReceivedData") or "").strip(),
            )
            hosts.setdefault(row["Host"], []).append(ping)
    return hosts


# --------------------------------------------------------------------- statistics


def percentile(values: list[float], fraction: float) -> float:
    """The smallest value that at least `fraction` of the values are at or below.

    This is the "inverted CDF" definition, and the one PingTracer uses for its
    worst 1% figure.  With fewer than 100 values the 0.99 percentile is the
    largest value.
    """
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def jitter(pings: list[Ping], times: list[float | None]) -> float:
    """Mean absolute change between consecutive replies; a loss breaks the pair."""
    total = 0.0
    pairs = 0
    previous = None
    for ping, value in zip(pings, times):
        if not ping.replied or value is None:
            previous = None
            continue
        if previous is not None:
            total += abs(value - previous)
            pairs += 1
        previous = value
    return total / pairs if pairs else math.nan


def find_outages(pings: list[Ping], min_lost: int = 2) -> list[Outage]:
    """Runs of at least `min_lost` lost pings in a row.

    An outage starts when its first lost ping was sent and ends when the first
    ping that got through afterwards was sent.
    """
    outages: list[Outage] = []
    current: Outage | None = None
    for ping in pings:
        if ping.replied:
            if current is not None:
                current.end = ping.sent
                if current.lost >= min_lost:
                    outages.append(current)
                current = None
        else:
            if current is None:
                current = Outage(start=ping.sent, end=ping.sent, lost=0)
            current.end = ping.sent
            current.lost += 1
    if current is not None and current.lost >= min_lost:
        current.ongoing = True
        outages.append(current)
    return outages


def differing_offsets(sent_hex: str, received_hex: str) -> list[int]:
    """Byte offsets at which two hex strings differ (compared up to the shorter)."""
    count = min(len(sent_hex), len(received_hex)) // 2
    return [i for i in range(count) if sent_hex[2 * i:2 * i + 2].lower() != received_hex[2 * i:2 * i + 2].lower()]


def find_mismatches(pings: list[Ping]) -> list[Mismatch]:
    """Replies whose data was not what was sent.

    A matching reply is exported with only the start of its data, so the two
    columns are only compared in full when the app itself flagged the ping.
    """
    found: list[Mismatch] = []
    for ping in pings:
        flagged = ping.result == "DataMismatch"
        visibly_different = (
            ping.replied and ping.sent_data and ping.received_data
            and ping.sent_data.lower() != ping.received_data.lower()
        )
        if flagged or visibly_different:
            found.append(Mismatch(
                sent=ping.sent,
                sent_bytes=ping.payload_bytes if ping.payload_bytes is not None else len(ping.sent_data) // 2,
                received_bytes=len(ping.received_data) // 2,
                differing_offsets=differing_offsets(ping.sent_data, ping.received_data),
            ))
    return found


def choose_times(pings: list[Ping], use_measured: bool) -> list[float | None]:
    """The latency of each ping: the operating system's figure unless asked otherwise."""
    if use_measured:
        return [p.measured_ms if p.replied else None for p in pings]
    return [p.rtt_ms if p.replied else None for p in pings]


def summarise(host: str, pings: list[Ping], min_outage: int = 2, use_measured: bool = False) -> Summary:
    summary = Summary(host=host, pings=len(pings))
    if not pings:
        return summary
    summary.first, summary.last = pings[0].sent, pings[-1].sent
    times = choose_times(pings, use_measured)
    values = [t for t in times if t is not None]
    summary.replies = sum(1 for p in pings if p.replied)
    summary.lost = summary.pings - summary.replies
    for ping in pings:
        if not ping.replied:
            summary.failure_kinds[ping.result or "(blank)"] = summary.failure_kinds.get(ping.result or "(blank)", 0) + 1
    if values:
        summary.baseline = min(values)
        summary.maximum = max(values)
        summary.mean = sum(values) / len(values)
        summary.median = percentile(values, 0.5)
        summary.p95 = percentile(values, 0.95)
        summary.p99 = percentile(values, 0.99)
        summary.queueing_mean = summary.mean - summary.baseline
        summary.jitter = jitter(pings, times)
    summary.outages = find_outages(pings, min_outage)
    summary.mismatches = find_mismatches(pings)
    return summary


def histogram(values: list[float], bin_ms: float | None = None) -> tuple[list[float], list[int], float]:
    """Counts per bin.  Returns (left edges, counts, bin width).

    Windows reports whole milliseconds, so the natural bin is 1 ms.  For a wide
    spread the bin widens to keep about 120 bins at most.
    """
    if not values:
        return [], [], bin_ms or 1.0
    low, high = min(values), max(values)
    if bin_ms is None:
        bin_ms = max(1.0, math.ceil((high - low + 1) / 120))
    start = math.floor(low / bin_ms) * bin_ms
    bins = int(math.floor((high - start) / bin_ms)) + 1
    counts = [0] * bins
    for value in values:
        counts[min(bins - 1, int((value - start) / bin_ms))] += 1
    return [start + i * bin_ms for i in range(bins)], counts, bin_ms


# ------------------------------------------------------------------------ reports


def duration_text(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s" if seconds < 10 else f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min {int(seconds % 60):02d} s"
    return f"{int(seconds // 3600)} h {int(seconds % 3600 // 60):02d} min"


def ms(value: float) -> str:
    if math.isnan(value):
        return "-"
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.2f}"


def text_histogram(values: list[float], bin_ms: float | None, width: int = 50) -> list[str]:
    edges, counts, step = histogram(values, bin_ms)
    if not counts:
        return ["  (no replies)"]
    peak = max(counts)
    lines = []
    for edge, count in zip(edges, counts):
        if count == 0:
            continue
        label = f"{edge:.0f}" if step == 1 else f"{edge:.0f}-{edge + step - 1:.0f}"
        bar = "#" * max(1, round(width * count / peak))
        lines.append(f"  {label:>11} ms | {bar} {count}")
    return lines


def report(summary: Summary, values: list[float], bin_ms: float | None, show_histogram: bool = True) -> str:
    s = summary
    out = [f"== {s.host}"]
    if not s.pings:
        return out[0] + "\n  no pings"
    span = (s.last - s.first).total_seconds() if s.first and s.last else 0
    out.append(f"  {s.pings} pings over {duration_text(span)}  ({s.first:%Y-%m-%d %H:%M:%S} to {s.last:%H:%M:%S})")
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(s.failure_kinds.items()))
    out.append(f"  lost {s.lost} ({s.loss_percent:.2f}%)" + (f": {kinds}" if kinds else ""))
    out.append(f"  latency ms: baseline {ms(s.baseline)}  median {ms(s.median)}  mean {ms(s.mean)}  "
               f"95% {ms(s.p95)}  worst 1% {ms(s.p99)}  max {ms(s.maximum)}")
    out.append(f"  jitter {ms(s.jitter)} ms   mean time above baseline (queueing) {ms(s.queueing_mean)} ms")
    if s.outages:
        total = sum(o.seconds for o in s.outages)
        out.append(f"  {len(s.outages)} outage(s), {duration_text(total)} in total, longest {duration_text(max(o.seconds for o in s.outages))}:")
        for o in s.outages:
            tail = "still down at end of file" if o.ongoing else f"until {o.end:%H:%M:%S}"
            out.append(f"    {o.start:%Y-%m-%d %H:%M:%S}  {duration_text(o.seconds):>10}  {o.lost} pings lost  ({tail})")
    else:
        out.append("  no outages")
    if s.mismatches:
        out.append(f"  {len(s.mismatches)} reply/replies came back with altered data:")
        for m in s.mismatches:
            where = (f"bytes {', '.join(map(str, m.differing_offsets[:12]))}"
                     + (" ..." if len(m.differing_offsets) > 12 else "")) if m.differing_offsets else "no differing byte in the part exported"
            length = "" if m.received_bytes == m.sent_bytes else f", {m.received_bytes} of {m.sent_bytes} bytes returned"
            out.append(f"    {m.sent:%Y-%m-%d %H:%M:%S}  {where}{length}")
    else:
        out.append("  all reply data matched what was sent")
    if show_histogram:
        out.append("  ping time histogram:")
        out.extend(text_histogram(values, bin_ms))
    return "\n".join(out)


# -------------------------------------------------------------------------- plots

# One blue for the data; everything else in ink and grey so the data stands out.
SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SERIES, CRITICAL = "#e1e0d9", "#c3c2b7", "#2a78d6", "#d03b3b"


def safe_name(host: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in host).strip("_") or "host"


def plot_host(summary: Summary, pings: list[Ping], times: list[float | None], path: Path, bin_ms: float | None = None) -> bool:
    """Draws the histogram and the over-time strip for one host.  Returns False without matplotlib."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    values = [t for t in times if t is not None]
    if not values:
        return False
    s = summary
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9, 6.4), dpi=130, gridspec_kw={"height_ratios": [3, 1.5], "hspace": 0.42})
    fig.patch.set_facecolor(SURFACE)
    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=9, length=0)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)

    # The slowest few pings can be far out; drawing the axis to reach them would squash everything else.
    spread = max(s.p99 - s.baseline, 1.0)
    limit = s.maximum if s.maximum <= s.p99 + 0.5 * spread else s.p99 + 0.25 * spread
    shown = [v for v in values if v <= limit]
    beyond = len(values) - len(shown)
    edges, counts, step = histogram(shown, bin_ms)
    top.bar([e + step / 2 for e in edges], counts, width=step * 0.86, color=SERIES, linewidth=0)
    top.set_xlim(edges[0] - step, edges[-1] + 2 * step)
    top.set_ylim(0, max(counts) * 1.22)
    top.set_xlabel("Ping time (ms)", color=INK_2, fontsize=10)
    top.set_ylabel("Pings", color=INK_2, fontsize=10)

    # Reference lines carry their own labels, staggered so they cannot overlap.
    markers = [("baseline", s.baseline), ("median", s.median), ("worst 1%", s.p99)]
    seen: list[float] = []
    for level, (name, value) in enumerate(markers):
        if value > limit or any(abs(value - other) < 1e-9 for other in seen):
            continue
        seen.append(value)
        top.axvline(value, color=INK_2, linewidth=1, linestyle=(0, (4, 3)))
        # The label sits on a patch of the surface colour so a neighbouring line cannot cut through it.
        top.text(value, max(counts) * (1.17 - 0.07 * level), f" {name} {ms(value)} ms", color=INK, fontsize=9, va="center", ha="left",
                 bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1.5}, zorder=5)
    if beyond:
        top.text(0.995, 0.5, f"{beyond} slower ping(s) not shown,\nup to {ms(s.maximum)} ms",
                 transform=top.transAxes, ha="right", va="center", color=INK_2, fontsize=9)

    fig.suptitle(s.host, x=0.07, y=0.975, ha="left", color=INK, fontsize=13)
    fig.text(0.07, 0.925, f"{s.pings} pings, {s.loss_percent:.2f}% lost, jitter {ms(s.jitter)} ms, "
             f"{len(s.outages)} outage(s), {len(s.mismatches)} altered reply/replies", color=INK_2, fontsize=10)

    start = pings[0].sent
    xs = [(p.sent - start).total_seconds() for p, t in zip(pings, times) if t is not None]
    bottom.plot(xs, values, color=SERIES, linewidth=1, solid_joinstyle="round")
    lost_x = [(p.sent - start).total_seconds() for p in pings if not p.replied]
    ceiling = max(values) * 1.12
    if lost_x:
        bottom.vlines(lost_x, 0, ceiling, color=CRITICAL, linewidth=1, label=f"lost ({len(lost_x)})")
        bottom.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=INK_2)
    bottom.set_ylim(0, ceiling)
    bottom.set_xlim(0, max((pings[-1].sent - start).total_seconds(), 1))
    bottom.set_xlabel(f"Seconds since {start:%H:%M:%S}", color=INK_2, fontsize=10)
    bottom.set_ylabel("ms", color=INK_2, fontsize=10)
    fig.subplots_adjust(left=0.07, right=0.975, top=0.88, bottom=0.09)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return True


# ---------------------------------------------------------------------- self-test


def write_sample(path: Path) -> None:
    """Writes an export with known contents: 500 pings a second apart."""
    start = datetime(2026, 11, 26, 21, 0, 0)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Host", "Sent", "Received", "Result", "RoundTripMs", "MeasuredMs", "PayloadBytes", "SentData", "ReceivedData"])
        for i in range(500):
            sent = start + timedelta(seconds=i)
            lost = i == 50 or 100 <= i <= 104 or i >= 497
            rtt = 20 + (i * 7) % 5 + (30 if i % 37 == 0 else 0) + (280 if i in (150, 300, 301) else 0)
            data = "55aa55aa55aa55aa"
            if lost:
                writer.writerow(["sample [192.0.2.1]", sent.strftime(TIME_FORMATS[0]), "", "TimedOut", "", "", 8, data, ""])
            elif i == 200:
                writer.writerow(["sample [192.0.2.1]", sent.strftime(TIME_FORMATS[0]), (sent + timedelta(milliseconds=rtt + 1.5)).strftime(TIME_FORMATS[0]),
                                 "DataMismatch", rtt, f"{rtt + 1.5:.3f}", 8, data, "55aa55ab55aa55aa"])
            else:
                writer.writerow(["sample [192.0.2.1]", sent.strftime(TIME_FORMATS[0]), (sent + timedelta(milliseconds=rtt + 1.5)).strftime(TIME_FORMATS[0]),
                                 "Success", rtt, f"{rtt + 1.5:.3f}", 8, data, data])
        # an older export has fewer columns; a second host exercises that path
        for i in range(60):
            writer.writerow(["steady, \"quoted\" [192.0.2.2]", (start + timedelta(seconds=i)).strftime(TIME_FORMATS[0]), "", "Success", 5, "", "", "", ""])


def self_test() -> int:
    failures: list[str] = []

    def check(name: str, got, want) -> None:
        ok = got == want or (isinstance(got, float) and isinstance(want, (int, float)) and math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9))
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got}" + ("" if ok else f"   (expected {want})"))
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory() as folder:
        sample = Path(folder) / "sample.csv"
        write_sample(sample)
        hosts = read_export(sample)
        check("hosts read", sorted(hosts), ['sample [192.0.2.1]', 'steady, "quoted" [192.0.2.2]'])
        pings = hosts["sample [192.0.2.1]"]
        s = summarise("sample [192.0.2.1]", pings)

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
        edges, counts, step = histogram([20, 20, 21, 23])
        check("histogram bins", (edges, counts, step), ([20.0, 21.0, 22.0, 23.0], [2, 1, 0, 1], 1.0))
        check("histogram counts add up", sum(histogram(values)[1]), len(values))

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
                check(f"{name}: this script {mine:g}, NumPy {theirs:g}", mine, theirs)
            # jitter by NumPy: differences between neighbours, keeping only pairs where neither ping was lost
            series = np.array([p.rtt_ms if p.replied else np.nan for p in pings], dtype=float)
            steps = np.abs(np.diff(series))
            check(f"jitter: this script {s.jitter:g}, NumPy {float(np.nanmean(steps)):g}", s.jitter, float(np.nanmean(steps)))
            np_counts, _ = np.histogram(arr, bins=np.arange(arr.min(), arr.max() + 2))
            check("histogram counts equal NumPy's", histogram(values, 1.0)[1] == [int(c) for c in np_counts], True)

        print(" older export format and plotting")
        steady = summarise('steady, "quoted" [192.0.2.2]', hosts['steady, "quoted" [192.0.2.2]'])
        check("old-format host: pings, loss, jitter", (steady.pings, steady.lost, steady.jitter), (60, 0, 0.0))
        picture = Path(folder) / "sample.png"
        drew = plot_host(s, pings, choose_times(pings, False), picture)
        if drew:
            check("histogram picture written", picture.stat().st_size > 10000, True)
        else:
            print("  matplotlib not installed: skipped drawing")
        print()
        print(report(s, values, None, show_histogram=False))

    print()
    print("SELF-TEST " + ("FAILED: " + ", ".join(failures) if failures else "PASSED"))
    return 1 if failures else 0


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyse a PingTracer CSV export.")
    parser.add_argument("export", nargs="?", help="CSV file from Export > Ping data to CSV")
    parser.add_argument("--out", help="folder for the histogram pictures (default: beside the CSV)")
    parser.add_argument("--min-outage", type=int, default=2, help="lost pings in a row that count as an outage (default 2)")
    parser.add_argument("--bin-ms", type=float, help="histogram bin width in ms (default: 1, wider for a large spread)")
    parser.add_argument("--use-measured", action="store_true", help="use the app's MeasuredMs instead of the operating system's RoundTripMs")
    parser.add_argument("--no-plots", action="store_true", help="print the report only")
    parser.add_argument("--self-test", action="store_true", help="run the built-in checks on generated data and exit")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.export:
        parser.error("give the path of a CSV export, or --self-test")

    hosts = read_export(args.export)
    out_dir = Path(args.out) if args.out else Path(args.export).resolve().parent
    for host, pings in hosts.items():
        summary = summarise(host, pings, args.min_outage, args.use_measured)
        times = choose_times(pings, args.use_measured)
        values = [t for t in times if t is not None]
        print(report(summary, values, args.bin_ms))
        if not args.no_plots:
            picture = out_dir / f"{Path(args.export).stem} - {safe_name(host)}.png"
            if plot_host(summary, pings, times, picture, args.bin_ms):
                print(f"  picture: {picture}")
            elif values:
                print("  (install matplotlib to get the histogram as a picture)")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
