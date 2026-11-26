#!/usr/bin/env python3
"""Finds the latency spikes in a PingTracer CSV export and helps look into them.

It numbers every moment at which something spiked (M1, M2, ...), lists each
host's spikes against those numbers, and then puts figures on how the hosts
relate to the first one in the file.

    py -3.15 ping_spikes.py "PingTracer 1.1.1.1 2026-11-26.csv"
    py -3.15 ping_spikes.py export.csv --above-ms 25 --plot

What counts as a spike: a ping slower than the host's median by more than
`--above-ms`.  Left unset, that margin is 10 ms or three times the spread of
the middle half of the pings (the interquartile range), whichever is larger,
so a naturally noisy host needs a bigger jump to count.  Spike pings less than
`--merge-seconds` apart are treated as one spike.  Spikes on different hosts
within `--tolerance-seconds` of each other belong to the same moment.

The figures given for each later host, against the first host in the file:

* together      how many of its spikes fell in a moment the first host shared
* by chance     how many would be expected to if the two were unrelated, from
                the share of the run the first host spent spiking, and the
                probability of seeing at least as many as were seen
* correlation   Pearson's r between the two hosts' ping times, round by round
                (+1 = they rise and fall together, 0 = unrelated)
* shared delay  of all this host's time above its own median, the percentage
                that was matched, in the same round, by time above the median
                on the first host

What these can and cannot tell you.  They measure how closely the hosts moved
together.  If a later host's delay is mostly shared with the first host, the
cause was at or before that first host; if little is shared, it was further
out.  That is a pointer for where to look, drawn from timing alone, not proof
of a cause.  The same goes for a regular interval between spikes.

The report needs only the standard library; --plot needs matplotlib.
"""

from __future__ import annotations

import argparse
import math
import statistics
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
    above_ms: float = 0.0          # how far the peak was above the host's median
    moment: int = 0                # number of the moment this spike belongs to (M1, M2, ...)
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


@dataclass
class Moment:
    number: int
    start: datetime
    end: datetime
    spikes: list[Spike]

    @property
    def hosts(self) -> list[str]:
        return list(dict.fromkeys(s.host for s in self.spikes))

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class Relation:
    """How one host's latency relates to the first host's."""
    host: str
    reference: str
    spikes: int = 0
    together: int = 0
    expected_by_chance: float = math.nan
    chance_probability: float = math.nan   # of at least `together` coinciding if the hosts were unrelated
    rounds_paired: int = 0
    correlation: float = math.nan
    shared_delay_percent: float = math.nan


# ----------------------------------------------------------------------- finding


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
    for spike in spikes:
        spike.above_ms = spike.peak_ms - median
    return HostSpikes(host=host, median_ms=median, threshold_ms=threshold, replies=len(values), spikes=spikes)


def number_moments(per_host: list[HostSpikes], tolerance_seconds: float = 0.5) -> list[Moment]:
    """Groups the spikes of every host into moments and numbers them in time order.

    A spike joins the current moment if it starts no later than `tolerance_seconds` after that
    moment's latest end, so spikes that overlap in a chain all share one number.  Each spike's
    `moment` and `shared_with` are filled in.
    """
    moments: list[Moment] = []
    for spike in sorted((s for h in per_host for s in h.spikes), key=lambda s: s.start):
        current = moments[-1] if moments else None
        if current is not None and (spike.start - current.end).total_seconds() <= tolerance_seconds:
            current.spikes.append(spike)
            current.end = max(current.end, spike.end)
        else:
            moments.append(Moment(number=len(moments) + 1, start=spike.start, end=spike.end, spikes=[spike]))
    for moment in moments:
        for spike in moment.spikes:
            spike.moment = moment.number
            spike.shared_with = [host for host in moment.hosts if host != spike.host]
    return moments


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


# -------------------------------------------------------------------- measuring


def pair_rounds(a: list[Ping], a_times: list[float | None], b: list[Ping], b_times: list[float | None]) -> list[tuple[float, float]]:
    """Pairs each reply on host B with the reply on host A sent in the same round.

    PingTracer sends to every host within a few milliseconds each round, so two pings belong to the
    same round if they were sent closer together than 0.4 of the usual gap between rounds.
    """
    sent_a = [(p.sent, t) for p, t in zip(a, a_times)]
    if len(sent_a) < 2:
        return []
    gaps = sorted((y[0] - x[0]).total_seconds() for x, y in zip(sent_a, sent_a[1:]))
    max_gap = 0.4 * gaps[len(gaps) // 2]
    pairs: list[tuple[float, float]] = []
    i = 0
    for ping, value in zip(b, b_times):
        while i + 1 < len(sent_a) and abs((sent_a[i + 1][0] - ping.sent).total_seconds()) <= abs((sent_a[i][0] - ping.sent).total_seconds()):
            i += 1
        other = sent_a[i]
        if value is not None and other[1] is not None and abs((other[0] - ping.sent).total_seconds()) <= max_gap:
            pairs.append((other[1], value))
    return pairs


def chance_of_at_least(successes: int, trials: int, probability: float) -> float:
    """Probability of at least `successes` in `trials` independent tries (the binomial upper tail)."""
    if trials <= 0:
        return math.nan
    probability = min(1.0, max(0.0, probability))
    return sum(math.comb(trials, k) * probability ** k * (1 - probability) ** (trials - k) for k in range(successes, trials + 1))


def relate(hosts: dict[str, list[Ping]], per_host: list[HostSpikes], tolerance_seconds: float = 0.5, use_measured: bool = False) -> list[Relation]:
    """Puts figures on how each later host relates to the first host in the file."""
    if len(per_host) < 2:
        return []
    first = per_host[0]
    first_pings = hosts[first.host]
    first_times = choose_times(first_pings, use_measured)
    run_seconds = (first_pings[-1].sent - first_pings[0].sent).total_seconds() if len(first_pings) > 1 else 0.0

    # The share of the run during which a spike elsewhere would count as "together" with the first host.
    covered = 0.0
    reach = None
    for spike in first.spikes:
        left = spike.start.timestamp() - tolerance_seconds
        right = spike.end.timestamp() + tolerance_seconds
        if reach is not None and left < reach:
            left = reach
        if right > left:
            covered += right - left
            reach = right
    share = min(1.0, covered / run_seconds) if run_seconds > 0 else math.nan

    relations: list[Relation] = []
    for other in per_host[1:]:
        relation = Relation(host=other.host, reference=first.host, spikes=len(other.spikes))
        relation.together = sum(1 for s in other.spikes if first.host in s.shared_with)
        if not math.isnan(share):
            relation.expected_by_chance = relation.spikes * share
            if relation.spikes:
                relation.chance_probability = chance_of_at_least(relation.together, relation.spikes, share)
        pairs = pair_rounds(first_pings, first_times, hosts[other.host], choose_times(hosts[other.host], use_measured))
        relation.rounds_paired = len(pairs)
        if len(pairs) >= 3:
            xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
            if len(set(xs)) > 1 and len(set(ys)) > 1:
                relation.correlation = statistics.correlation(xs, ys)
            mine = [max(0.0, y - other.median_ms) for y in ys]
            theirs = [max(0.0, x - first.median_ms) for x in xs]
            if sum(mine) > 0:
                relation.shared_delay_percent = 100.0 * sum(min(m, t) for m, t in zip(mine, theirs)) / sum(mine)
        relations.append(relation)
    return relations


def analyse(hosts: dict[str, list[Ping]], above_ms: float | None = None, merge_seconds: float = 1.0,
            tolerance_seconds: float = 0.5, use_measured: bool = False) -> list[HostSpikes]:
    """Finds every host's spikes and numbers the moments.  Use list_moments() and relate() on the result."""
    per_host = [find_spikes(host, pings, choose_times(pings, use_measured), above_ms, merge_seconds) for host, pings in hosts.items()]
    number_moments(per_host, tolerance_seconds)
    return per_host


def list_moments(per_host: list[HostSpikes]) -> list[Moment]:
    """The numbered moments, rebuilt from the numbers analyse() gave each spike."""
    by_number: dict[int, Moment] = {}
    for spike in sorted((s for h in per_host for s in h.spikes), key=lambda s: s.start):
        moment = by_number.get(spike.moment)
        if moment is None:
            by_number[spike.moment] = Moment(number=spike.moment, start=spike.start, end=spike.end, spikes=[spike])
        else:
            moment.spikes.append(spike)
            moment.end = max(moment.end, spike.end)
    return [by_number[number] for number in sorted(by_number)]


# --------------------------------------------------------------------- reporting


def probability_text(value: float) -> str:
    if math.isnan(value):
        return "-"
    if value >= 0.01:
        return f"{value:.2f}"
    if value >= 0.0001:
        return f"{value:.4f}"
    return f"{value:.1e}"


def report(per_host: list[HostSpikes], relations: list[Relation] | None = None, max_listed: int = 40) -> str:
    out: list[str] = []
    moments = list_moments(per_host)
    host_count = sum(1 for h in per_host if h.replies)
    if moments:
        shared = sum(1 for m in moments if len(m.hosts) > 1)
        out.append(f"Moments: {len(moments)} in all, {shared} on more than one host   (peak above each host's own median)")
        for m in moments[:max_listed]:
            peaks = " | ".join(f"{s.host} +{ms(s.above_ms)} ms" for s in m.spikes)
            out.append(f"  M{m.number:<3} {m.start:%H:%M:%S.%f}"[:-3] + f"  {duration_text(m.seconds):>6}  {len(m.hosts)} of {host_count} hosts   {peaks}")
        if len(moments) > max_listed:
            out.append(f"  ... and {len(moments) - max_listed} more")
        out.append("")
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
            out.append(f"  spacing: regular, every {duration_text(rhythm[0])}, varying by {100 * rhythm[1]:.0f}% (a pointer to something on a timer)")
        else:
            out.append(f"  spacing: irregular, typically {duration_text(rhythm[0])} apart but varying by {100 * rhythm[1]:.0f}%")
        for s in h.spikes[:max_listed]:
            length = "1 ping" if s.pings == 1 else f"{s.pings} pings, {duration_text(s.seconds)}"
            also = ("also: " + ", ".join(s.shared_with)) if s.shared_with else "this host only"
            out.append(f"    M{s.moment:<3} {s.start:%H:%M:%S.%f}"[:-3] + f"  peak {ms(s.peak_ms):>5} ms (+{ms(s.above_ms)})  {length:<16} {also}")
        if len(h.spikes) > max_listed:
            out.append(f"    ... and {len(h.spikes) - max_listed} more")
    if relations:
        out.append("")
        out.append(f"Measured against the first host ({relations[0].reference}):")
        for r in relations:
            out.append(f"  {r.host}")
            if r.spikes:
                out.append(f"    spikes together   {r.together} of {r.spikes} ({100 * r.together / r.spikes:.0f}%)")
                out.append(f"    by chance         {r.expected_by_chance:.2f} expected if unrelated; probability of {r.together} or more: {probability_text(r.chance_probability)}")
            else:
                out.append("    spikes together   no spikes on this host")
            corr = "-" if math.isnan(r.correlation) else f"{r.correlation:+.2f}"
            out.append(f"    correlation       r = {corr} over {r.rounds_paired} rounds")
            share = "-" if math.isnan(r.shared_delay_percent) else f"{r.shared_delay_percent:.0f}%"
            out.append(f"    shared delay      {share} of this host's time above its median was matched on the first host")
        out.append("  High figures point at or before the first host; low ones point further out. Timing only, not proof.")
    return "\n".join(out)


# ------------------------------------------------------------------------- plots

# One blue for the pings, one orange for the spikes; everything else in ink and grey.
SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SERIES, ACCENT, BAND = "#e1e0d9", "#c3c2b7", "#2a78d6", "#eb6834", "#eceae3"


def figure_spikes(hosts: dict[str, list[Ping]], per_host: list[HostSpikes], use_measured: bool = False, max_labels: int = 40):
    """One row per host on a shared time axis, spikes marked and numbered, shared moments shaded across every row.

    Returns a matplotlib figure, or None if no host had a reply.  plot_spikes() saves it to a file instead.
    """
    import matplotlib.pyplot as plt

    rows = [h for h in per_host if h.replies]
    if not rows:
        return None
    start = min(pings[0].sent for pings in hosts.values() if pings)
    fig, axes = plt.subplots(len(rows), 1, figsize=(9, 1.2 + 1.9 * len(rows)), dpi=130, sharex=True, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    moments = list_moments(per_host)
    shared = [m for m in moments if len(m.hosts) > 1]
    label_spikes = len(moments) <= max_labels
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
        for m in shared:
            ax.axvspan((m.start - start).total_seconds() - 0.4, (m.end - start).total_seconds() + 0.4, color=BAND, linewidth=0, zorder=0)
        xs = [(p.sent - start).total_seconds() for p, t in zip(pings, times) if t is not None]
        ys = [t for t in times if t is not None]
        top = max(ys) * 1.3
        ax.plot(xs, ys, color=SERIES, linewidth=1, solid_joinstyle="round", zorder=2)
        sx = [x for x, y in zip(xs, ys) if y > h.threshold_ms]
        sy = [y for y in ys if y > h.threshold_ms]
        ax.plot(sx, sy, linestyle="none", marker="D", markersize=4.5, color=ACCENT, markeredgecolor=SURFACE, markeredgewidth=0.8,
                zorder=3, label=f"spike ({len(h.spikes)})")
        ax.axhline(h.threshold_ms, color=INK_2, linewidth=0.8, linestyle=(0, (4, 3)), zorder=1)
        if label_spikes:
            # Each spike carries the number of its moment, just above its peak.
            # A label too close to the one before it steps up a line, so neighbours never sit on top of each other.
            too_close = 0.035 * max(xs[-1] - xs[0], 1.0)
            previous_middle, raised = None, False
            for s in h.spikes:
                middle = ((s.start - start).total_seconds() + (s.end - start).total_seconds()) / 2
                raised = previous_middle is not None and middle - previous_middle < too_close and not raised
                previous_middle = middle
                ax.annotate(f"M{s.moment}", (middle, s.peak_ms), xytext=(0, 19 if raised else 7), textcoords="offset points", ha="center", va="bottom",
                            fontsize=8, color=INK, zorder=4, bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 0.8, "alpha": 0.85})
        ax.set_ylim(0, top)
        ax.set_ylabel("ms", color=INK_2, fontsize=10)
        ax.set_title(f"{h.host}   median {ms(h.median_ms)} ms, spike above {ms(h.threshold_ms)} ms", loc="left", color=INK, fontsize=10, pad=6)
        ax.legend(loc="upper right", frameon=False, fontsize=9, labelcolor=INK_2, handletextpad=0.2)
    axes[-1, 0].set_xlabel(f"Seconds since {start:%H:%M:%S}    (shaded: a moment shared by more than one host)", color=INK_2, fontsize=10)
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
    parser = argparse.ArgumentParser(description="Find, number and compare the latency spikes in a PingTracer CSV export.")
    parser.add_argument("export", help="CSV file from Export > Ping data to CSV")
    parser.add_argument("--above-ms", type=float, help="how far above the median counts as a spike (default: 10 ms or 3x the interquartile range, whichever is larger)")
    parser.add_argument("--merge-seconds", type=float, default=1.0, help="spike pings closer together than this are one spike (default 1)")
    parser.add_argument("--tolerance-seconds", type=float, default=0.5, help="how close in time spikes on two hosts must be to share a moment (default 0.5)")
    parser.add_argument("--use-measured", action="store_true", help="use the app's MeasuredMs instead of the operating system's RoundTripMs")
    parser.add_argument("--plot", action="store_true", help="also draw every host over time with the spikes marked and numbered")
    parser.add_argument("--out", help="folder for the picture (default: beside the CSV)")
    args = parser.parse_args(argv)

    hosts = read_export(args.export)
    per_host = analyse(hosts, args.above_ms, args.merge_seconds, args.tolerance_seconds, args.use_measured)
    relations = relate(hosts, per_host, args.tolerance_seconds, args.use_measured)
    print(report(per_host, relations))
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
