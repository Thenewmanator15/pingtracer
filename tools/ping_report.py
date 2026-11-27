#!/usr/bin/env python3
"""Prints a report on a PingTracer CSV export (Export > Ping data to CSV).

For every host: latency statistics, loss, each drop-out with its start and
length, and any reply whose data came back altered.

    py -3.15 ping_report.py "PingTracer 1.1.1.1 2026-11-26.csv"
    py -3.15 ping_report.py export.csv --min-outage 3 --text-histogram

Needs only the standard library.  For pictures, see ping_histogram.py.
"""

from __future__ import annotations

import argparse
import sys

from ping_export import Summary, choose_times, duration_text, histogram, ms, read_export, summarise


def text_histogram(values: list[float], bin_ms: float | None, width: int = 50) -> list[str]:
    edges, counts, step = histogram(values, bin_ms)
    if not counts:
        return ["  (no replies)"]
    peak = max(counts)
    lines = []
    for edge, count in zip(edges, counts):
        if count == 0:
            continue
        if step < 1:
            label = f"{edge:g}"
        else:
            label = f"{edge:.0f}" if step == 1 else f"{edge:.0f}-{edge + step - 1:.0f}"
        bar = "#" * max(1, round(width * count / peak))
        lines.append(f"  {label:>11} ms | {bar} {count}")
    return lines


def report(summary: Summary) -> str:
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
        longest = max(o.seconds for o in s.outages)
        out.append(f"  {len(s.outages)} outage(s), {duration_text(total)} in total, longest {duration_text(longest)}:")
        for o in s.outages:
            tail = "still down at end of file" if o.ongoing else f"until {o.end:%H:%M:%S}"
            out.append(f"    {o.start:%Y-%m-%d %H:%M:%S}  {duration_text(o.seconds):>10}  {o.lost} pings lost  ({tail})")
    else:
        out.append("  no outages")
    if s.mismatches:
        out.append(f"  {len(s.mismatches)} reply/replies came back with altered data:")
        for m in s.mismatches:
            if m.differing_offsets:
                where = f"bytes {', '.join(map(str, m.differing_offsets[:12]))}" + (" ..." if len(m.differing_offsets) > 12 else "")
            else:
                where = "no differing byte in the part exported"
            length = "" if m.received_bytes == m.sent_bytes else f", {m.received_bytes} of {m.sent_bytes} bytes returned"
            out.append(f"    {m.sent:%Y-%m-%d %H:%M:%S}  {where}{length}")
    elif s.payload_checked:
        out.append("  all reply data matched what was sent")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a report on a PingTracer CSV export.")
    parser.add_argument("export", help="CSV file from Export > Ping data to CSV")
    parser.add_argument("--min-outage", type=int, default=2, help="lost pings in a row that count as an outage (default 2)")
    parser.add_argument("--use-measured", action="store_true", help="use the app's MeasuredMs instead of the operating system's RoundTripMs")
    parser.add_argument("--text-histogram", action="store_true", help="also print a histogram of ping times as text")
    parser.add_argument("--bin-ms", type=float, help="bin width for --text-histogram (default: 1, wider for a large spread)")
    args = parser.parse_args(argv)

    for host, pings in read_export(args.export).items():
        summary = summarise(host, pings, args.min_outage, args.use_measured)
        print(report(summary))
        if args.text_histogram:
            values = [t for t in choose_times(pings, args.use_measured) if t is not None]
            print("  ping time histogram:")
            print("\n".join(text_histogram(values, args.bin_ms)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
