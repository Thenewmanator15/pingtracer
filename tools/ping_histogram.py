#!/usr/bin/env python3
"""Draws a histogram of ping times for each host in a PingTracer CSV export.

Each picture has the histogram on top, with the baseline, median and worst 1%
marked, and the pings over time underneath with lost pings in red.

    py -3.15 ping_histogram.py "PingTracer 1.1.1.1 2026-11-26.csv"
    py -3.15 ping_histogram.py export.csv --out plots --bin-ms 5

Needs matplotlib.  For the numbers alone, see ping_report.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ping_export import Ping, Summary, choose_times, histogram, ms, read_export, summarise

# One blue for the data; everything else in ink and grey so the data stands out.
SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SERIES, CRITICAL = "#e1e0d9", "#c3c2b7", "#2a78d6", "#d03b3b"


def safe_name(host: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in host).strip("_") or "host"


def figure_host(summary: Summary, pings: list[Ping], times: list[float | None], bin_ms: float | None = None):
    """Builds the histogram and the over-time strip for one host as a matplotlib figure.

    Returns None if there were no replies to draw.  In a notebook the figure shows itself;
    plot_host() saves it to a file instead.
    """
    import matplotlib.pyplot as plt

    values = [t for t in times if t is not None]
    if not values:
        return None
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
    return fig


def plot_host(summary: Summary, pings: list[Ping], times: list[float | None], path: Path, bin_ms: float | None = None) -> bool:
    """Draws one host's picture to a file.  Returns False if there were no replies to draw."""
    import matplotlib.pyplot as plt

    fig = figure_host(summary, pings, times, bin_ms)
    if fig is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draw a histogram of ping times for each host in a PingTracer CSV export.")
    parser.add_argument("export", help="CSV file from Export > Ping data to CSV")
    parser.add_argument("--out", help="folder for the pictures (default: beside the CSV)")
    parser.add_argument("--bin-ms", type=float, help="bin width in ms (default: 1, wider for a large spread)")
    parser.add_argument("--min-outage", type=int, default=2, help="lost pings in a row that count as an outage (default 2)")
    parser.add_argument("--use-measured", action="store_true", help="use the app's MeasuredMs instead of the operating system's RoundTripMs")
    args = parser.parse_args(argv)

    try:
        import matplotlib
        matplotlib.use("Agg")  # drawing to files only; no window needed
    except ImportError:
        print("This script needs matplotlib.  Install it, or use ping_report.py --text-histogram.", file=sys.stderr)
        return 2

    out_dir = Path(args.out) if args.out else Path(args.export).resolve().parent
    for host, pings in read_export(args.export).items():
        summary = summarise(host, pings, args.min_outage, args.use_measured)
        picture = out_dir / f"{Path(args.export).stem} - {safe_name(host)}.png"
        if plot_host(summary, pings, choose_times(pings, args.use_measured), picture, args.bin_ms):
            print(picture)
        else:
            print(f"{host}: no replies, nothing to draw")
    return 0


if __name__ == "__main__":
    sys.exit(main())
