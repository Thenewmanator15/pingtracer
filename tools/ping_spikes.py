#!/usr/bin/env python3
"""Finds the latency spikes in a PingTracer CSV export and helps look into them.

For every host it lists each spike (when, how long, how high), then looks
across the hosts for moments when several spiked together, and at whether the
spikes come at a regular interval.

    py -3.13 ping_spikes.py "PingTracer 1.1.1.1 2026-11-26.csv"
    py -3.13 ping_spikes.py export.csv --above-ms 25 --plot

What counts as a spike: a ping slower than the host's median by more than
`--above-ms`.  Left unset, that margin is 10 ms or three times the spread of
the middle half of the pings (the interquartile range), whichever is larger,
so a naturally noisy host needs a bigger jump to count.  Spike pings less than
`--merge-seconds` apart are treated as one spike.

What it can and cannot tell you.  It reports what coincided.  If a spike on the
last host shows up at the same moment on the first host too, whatever caused it
was at or before that first host; if only the later hosts spiked, it was
further out.  That is a pointer for where to look, drawn from timing alone, not
proof of a cause.  The same goes for a regular interval between spikes.

The report needs only the standard library; --plot needs matplotlib.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ping_export import Ping, choose_times, duration_text, ms, percentile, read_export


@dataclass
class Spike:
    host: str
    start: datetime
    end: datetime
    pings: int
    peak_ms: float
    shared_with: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class HostSpikes:
    host: str
    median_ms: float
    threshold_ms: float
    replies: int
    spikes: list[Spike]


def spike_threshold(values: list[float], above_ms: float | None = None, floor_ms: float = 10.0) -> tuple[float, float]:
    """Returns (median, threshold).  A ping above the threshold is a spike."""
    if not values:
        return math.nan, math.nan
    median = percentile(values, 0.5)
    if above_ms is None:
        spread = percentile(values, 0.75) - percentile(values, 0.25)
        above_ms = max(floor_ms, 3 * spread)
    return median, median + above_ms


def find_spikes(host: str, pings: list[Ping], times: list[float | None], above_ms: float | None = None, merge_seconds: float = 1.0) -> HostSpikes:
    values = [t for t in times if t is not None]
    median, threshold = spike_threshold(values, above_ms)
    spikes: list[Spike] = []
    for ping, value in zip(pings, times):
        if value is None or not value > threshold:
            continue
        last = spikes[-1] if spikes else None
        if last is not None and (ping.sent - last.end).total_seconds() <= merge_seconds:
            last.end = ping.sent
            last.pings += 1
            last.peak_ms = max(last.peak_ms, value)
        else:
            spikes.append(Spike(host=host, start=ping.sent, end=ping.sent, pings=1, peak_ms=value))
    return HostSpikes(host=host, median_ms=median, threshold_ms=threshold, replies=len(values), spikes=spikes)


def mark_shared(per_host: list[HostSpikes], tolerance_seconds: float = 0.5) -> None:
    """Fills in, for every spike, which other hosts spiked at the same moment."""
    for mine in per_host:
        for spike in mine.spikes:
            spike.shared_with = []
            for other in per_host:
                if other is mine:
                    continue
                for theirs in other.spikes:
                    apart = max((theirs.start - spike.end).total_seconds(), (spike.start - theirs.end).total_seconds())
                    if apart <= tolerance_seconds:
                        spike.shared_with.append(other.host)
                        break


def regularity(spikes: list[Spike]) -> tuple[float, float] | None:
    """Returns (typical seconds between spikes, how much that varies as a fraction), or None with fewer than 4 spikes.

    The variation is the interquartile range of the gaps divided by their median: near 0 means evenly spaced.
    """
    if len(spikes) < 4:
        return None
    gaps = [(b.start - a.start).total_seconds() for a, b in zip(spikes, spikes[1:])]
    typical = percentile(gaps, 0.5)
    if typical <= 0:
        return None
    variation = (percentile(gaps, 0.75) - percentile(gaps, 0.25)) / typical
    return typical, variation


def analyse(hosts: dict[str, list[Ping]], above_ms: float | None = None, merge_seconds: float = 1.0,
            tolerance_seconds: float = 0.5, use_measured: bool = False) -> list[HostSpikes]:
    per_host = [find_spikes(host, pings, choose_times(pings, use_measured), above_ms, merge_seconds) for host, pings in hosts.items()]
    mark_shared(per_host, tolerance_seconds)
    return per_host


def report(per_host: list[HostSpikes], max_listed: int = 40) -> str:
    out: list[str] = []
    for h in per_host:
        out.append(f"== {h.host}")
        if not h.replies:
            out.append("  no replies")
            continue
        out.append(f"  median {ms(h.median_ms)} ms; a spike is any ping above {ms(h.threshold_ms)} ms")
        if not h.spikes:
            out.append("  no spikes")
            continue
        alone = sum(1 for s in h.spikes if not s.shared_with)
        out.append(f"  {len(h.spikes)} spike(s), highest {ms(max(s.peak_ms for s in h.spikes))} ms; "
                   f"{len(h.spikes) - alone} at the same moment as another host, {alone} on this host alone")
        rhythm = regularity(h.spikes)
        if rhythm is None:
            out.append("  too few spikes to say whether they are regular")
        elif rhythm[1] <= 0.2:
            out.append(f"  spacing: regular, about every {duration_text(rhythm[0])} (a pointer to something on a timer)")
        else:
            out.append(f"  spacing: irregular (typically {duration_text(rhythm[0])} apart, but varying a lot)")
        for s in h.spikes[:max_listed]:
            length = "1 ping" if s.pings == 1 else f"{s.pings} pings, {duration_text(s.seconds)}"
            also = ("also: " + ", ".join(s.shared_with)) if s.shared_with else "this host only"
            out.append(f"    {s.start:%Y-%m-%d %H:%M:%S.%f}"[:-3] + f"  peak {ms(s.peak_ms):>5} ms  {length:<16} {also}")
        if len(h.spikes) > max_listed:
            out.append(f"    ... and {len(h.spikes) - max_listed} more")
    if len(per_host) > 1:
        first = per_host[0]
        later = [s for h in per_host[1:] for s in h.spikes]
        with_first = sum(1 for s in later if first.host in s.shared_with)
        out.append("")
        out.append(f"Across hosts: of {len(later)} spike(s) on the later hosts, {with_first} happened at the same moment as a spike on "
                   f"the first host ({first.host}).")
        out.append("  Those point at or before the first host; the rest point further out. Timing only, not proof.")
    return "\n".join(out)


# One blue for the pings, one orange for the spikes; everything else in ink and grey.
SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SERIES, ACCENT, BAND = "#e1e0d9", "#c3c2b7", "#2a78d6", "#eb6834", "#eceae3"


def figure_spikes(hosts: dict[str, list[Ping]], per_host: list[HostSpikes], use_measured: bool = False):
    """One row per host on a shared time axis, spikes marked, shared moments shaded across every row.

    Returns a matplotlib figure, or None if no host had a reply.  plot_spikes() saves it to a file instead.
    """
    import matplotlib.pyplot as plt

    rows = [h for h in per_host if h.replies]
    if not rows:
        return None
    start = min(pings[0].sent for pings in hosts.values() if pings)
    fig, axes = plt.subplots(len(rows), 1, figsize=(9, 1.2 + 1.9 * len(rows)), dpi=130, sharex=True, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    shared = [s for h in rows for s in h.spikes if s.shared_with]
    for ax, h in zip(axes[:, 0], rows):
        pings = hosts[h.host]
        times = choose_times(pings, use_measured)
        ax.set_facecolor(SURFACE)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=9, length=0)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for s in shared:
            left = (s.start - start).total_seconds() - 0.4
            ax.axvspan(left, (s.end - start).total_seconds() + 0.4, color=BAND, linewidth=0, zorder=0)
        xs = [(p.sent - start).total_seconds() for p, t in zip(pings, times) if t is not None]
        ys = [t for t in times if t is not None]
        ax.plot(xs, ys, color=SERIES, linewidth=1, solid_joinstyle="round", zorder=2)
        sx = [x for x, y in zip(xs, ys) if y > h.threshold_ms]
        sy = [y for y in ys if y > h.threshold_ms]
        ax.plot(sx, sy, linestyle="none", marker="D", markersize=4.5, color=ACCENT, markeredgecolor=SURFACE, markeredgewidth=0.8,
                zorder=3, label=f"spike ({len(h.spikes)})")
        ax.axhline(h.threshold_ms, color=INK_2, linewidth=0.8, linestyle=(0, (4, 3)), zorder=1)
        ax.set_ylim(0, max(ys) * 1.18)
        ax.set_ylabel("ms", color=INK_2, fontsize=10)
        ax.set_title(f"{h.host}   median {ms(h.median_ms)} ms, spike above {ms(h.threshold_ms)} ms", loc="left", color=INK, fontsize=10, pad=6)
        ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=INK_2, handletextpad=0.2)
    axes[-1, 0].set_xlabel(f"Seconds since {start:%H:%M:%S}    (shaded: a spike on more than one host at once)", color=INK_2, fontsize=10)
    fig.subplots_adjust(left=0.07, right=0.975, top=1 - 0.45 / fig.get_figheight(), bottom=0.62 / fig.get_figheight(), hspace=0.42)
    return fig


def plot_spikes(hosts: dict[str, list[Ping]], per_host: list[HostSpikes], path: Path, use_measured: bool = False) -> bool:
    """Draws the spike picture to a file.  Returns False if no host had a reply."""
    import matplotlib.pyplot as plt

    fig = figure_spikes(hosts, per_host, use_measured)
    if fig is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find and compare the latency spikes in a PingTracer CSV export.")
    parser.add_argument("export", help="CSV file from Export > Ping data to CSV")
    parser.add_argument("--above-ms", type=float, help="how far above the median counts as a spike (default: 10 ms or 3x the interquartile range, whichever is larger)")
    parser.add_argument("--merge-seconds", type=float, default=1.0, help="spike pings closer together than this are one spike (default 1)")
    parser.add_argument("--tolerance-seconds", type=float, default=0.5, help="how close in time spikes on two hosts must be to count as together (default 0.5)")
    parser.add_argument("--use-measured", action="store_true", help="use the app's MeasuredMs instead of the operating system's RoundTripMs")
    parser.add_argument("--plot", action="store_true", help="also draw every host over time with the spikes marked")
    parser.add_argument("--out", help="folder for the picture (default: beside the CSV)")
    args = parser.parse_args(argv)

    hosts = read_export(args.export)
    per_host = analyse(hosts, args.above_ms, args.merge_seconds, args.tolerance_seconds, args.use_measured)
    print(report(per_host))
    if args.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")  # drawing to a file only; no window needed
        except ImportError:
            print("\n--plot needs matplotlib.", file=sys.stderr)
            return 2
        out_dir = Path(args.out) if args.out else Path(args.export).resolve().parent
        picture = out_dir / f"{Path(args.export).stem} - spikes.png"
        if plot_spikes(hosts, per_host, picture, args.use_measured):
            print(f"\npicture: {picture}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
