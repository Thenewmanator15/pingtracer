#!/usr/bin/env python3
"""Runs twampy and turns every probe into statistics for each direction.

twampy itself prints one summary table.  This runs its sender in verbose mode,
reads the line it logs for every probe, and gives the full statistics for the
way out, the way back and the round trip: baseline, median, mean, 95%, worst
1%, maximum, jitter, loss, and any outages.  The probes are also saved as a
CSV in the same shape as a PingTracer export, with the three directions as
three "hosts", so ping_histogram.py and ping_spikes.py work on it as they are.

On the far machine, from inside twampy's src folder:

    python -m twampy responder :20001

On this machine:

    py -3.15 twampy_stats.py 192.168.1.233:20001 --count 600 --interval 100 --plots

To analyse a verbose twampy log you already have:

    py -3.15 twampy_stats.py --from-log twampy.log --csv run.csv

Things to keep in mind:

* twampy shows any delay below zero as zero.  If the two machines' clocks
  disagree, one direction can pile up at exactly 0; this script says so when
  it sees it.  The round trip is not affected.
* The figures come from twampy's log, which is rounded to 0.01 ms.
* When a probe goes missing, which direction lost it is worked out from the
  reflector's own count, the same way twampy does.  A probe lost after the
  last reply received cannot be placed and is counted as "direction unknown".
"""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ping_export import duration_text, read_export, summarise
from ping_report import report

TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
DEFAULT_TWAMPY_SRC = Path(__file__).resolve().parent.parent / "dist" / "third-party" / "twampy" / "src"
STAMP = r"(\d\d/\d\d/\d\d \d\d:\d\d:\d\d),(\d+)\s+INFO\s+"
SENT_LINE = re.compile(STAMP + r"Sent to (\S+) \[sseq=(\d+)\]")
REPLY_LINE = re.compile(STAMP + r"Reply from (\S+) \[rseq=(\d+) sseq=(\d+) rtt=([\d.]+)ms outbound=([\d.]+)ms inbound=([\d.]+)ms\]")
# Runs twampy's command line straight from its source folder, without installing it.
BOOTSTRAP = "import sys; sys.path.insert(0, sys.argv.pop(1)); sys.argv[0] = 'twampy'; from twampy.__main__ import main; main()"

# What happened to a probe
ANSWERED, LOST_OUT, LOST_BACK, LOST_UNKNOWN = "answered", "lost on the way out", "lost on the way back", "lost, direction unknown"


@dataclass
class Probe:
    sequence: int
    sent: datetime | None = None
    fate: str = LOST_UNKNOWN
    reflector_sequence: int | None = None
    round_trip_ms: float | None = None
    outbound_ms: float | None = None
    inbound_ms: float | None = None


def line_time(stamp: str, milliseconds: str) -> datetime:
    return datetime.strptime(stamp, "%y/%m/%d %H:%M:%S") + timedelta(milliseconds=int(milliseconds))


def parse_log(lines) -> tuple[list[Probe], str]:
    """Reads twampy's verbose sender log.  Returns the probes in order, and the far end's address."""
    probes: dict[int, Probe] = {}
    far_end = ""
    for line in lines:
        sent = SENT_LINE.search(line)
        if sent:
            sequence = int(sent.group(4))
            far_end = far_end or sent.group(3)
            probes.setdefault(sequence, Probe(sequence)).sent = line_time(sent.group(1), sent.group(2))
            continue
        reply = REPLY_LINE.search(line)
        if reply:
            sequence = int(reply.group(5))
            probe = probes.setdefault(sequence, Probe(sequence))
            probe.fate = ANSWERED
            probe.reflector_sequence = int(reply.group(4))
            probe.round_trip_ms = float(reply.group(6))
            probe.outbound_ms = float(reply.group(7))
            probe.inbound_ms = float(reply.group(8))
            if probe.sent is None:
                probe.sent = line_time(reply.group(1), reply.group(2)) - timedelta(milliseconds=probe.round_trip_ms)
    ordered = [probes[k] for k in sorted(probes)]
    place_losses(ordered)
    return ordered, far_end


def place_losses(probes: list[Probe]) -> None:
    """Decides, for each missing probe, which direction lost it.

    The reflector numbers the probes it receives.  Between two answered probes, the reflector's number
    advancing by more than one means it received probes whose replies never arrived: lost on the way
    back.  Any other missing probes in that stretch never reached it: lost on the way out.  Which
    individual probe is which cannot be known, only how many, so the stretch's "back" losses are put
    first.  Probes missing before the first reply are placed the same way, counting from zero.
    """
    previous_reflector = -1
    missing: list[Probe] = []
    for probe in probes:
        if probe.fate != ANSWERED:
            missing.append(probe)
            continue
        reached_reflector = probe.reflector_sequence - previous_reflector - 1
        for index, lost in enumerate(missing):
            lost.fate = LOST_BACK if index < reached_reflector else LOST_OUT
        missing = []
        previous_reflector = probe.reflector_sequence
    # anything still in `missing` came after the last reply and stays "direction unknown"


def write_csv(probes: list[Probe], far_end: str, path: str | Path) -> None:
    """Writes the probes as a PingTracer-style export, each direction as its own host.

    A probe only appears under a direction if something is known about it there: one lost on the way
    out is a loss for "outbound" and "round trip" but is left out of "inbound", where it was never sent.
    """
    directions = (
        ("outbound", lambda p: p.outbound_ms, (LOST_OUT,)),
        ("inbound", lambda p: p.inbound_ms, (LOST_BACK,)),
        ("round trip", lambda p: p.round_trip_ms, (LOST_OUT, LOST_BACK, LOST_UNKNOWN)),
    )
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Host", "Sent", "Received", "Result", "RoundTripMs", "MeasuredMs", "PayloadBytes", "SentData", "ReceivedData"])
        for name, value, lost_here in directions:
            for p in probes:
                if p.sent is None:
                    continue
                sent = p.sent.strftime(TIME_FORMAT)
                if p.fate == ANSWERED:
                    received = (p.sent + timedelta(milliseconds=p.round_trip_ms)).strftime(TIME_FORMAT)
                    writer.writerow([f"{far_end} {name}", sent, received, "Success", f"{value(p):.2f}", f"{p.round_trip_ms:.2f}", "", "", ""])
                elif p.fate in lost_here:
                    writer.writerow([f"{far_end} {name}", sent, "", "TimedOut", "", "", "", "", ""])


def run_twampy(far_end: str, near_end: str, count: int, interval_ms: int, twampy_src: Path, echo: bool = True) -> list[str]:
    """Runs twampy's sender in verbose mode and returns everything it printed."""
    if not (twampy_src / "twampy" / "__main__.py").exists():
        raise SystemExit(f"twampy was not found in {twampy_src}. Give its src folder with --twampy-src.")
    command = [sys.executable, "-I", "-c", BOOTSTRAP, str(twampy_src), "sender", far_end, near_end, "-c", str(count), "-i", str(interval_ms), "-v"]
    lines: list[str] = []
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    assert process.stdout is not None
    for line in process.stdout:
        lines.append(line.rstrip("\n"))
        if echo and " INFO " not in line:
            print(line.rstrip("\n"))   # twampy's own summary table and any errors; the per-probe lines are kept quiet
    process.wait()
    return lines


def statistics(probes: list[Probe], far_end: str, csv_path: str | Path, min_outage: int = 2) -> str:
    """The full report for a run: counts, then statistics for each direction."""
    total = len(probes)
    fates = {fate: sum(1 for p in probes if p.fate == fate) for fate in (ANSWERED, LOST_OUT, LOST_BACK, LOST_UNKNOWN)}
    out = [f"{total} probes to {far_end}: {fates[ANSWERED]} answered"]
    if total:
        span = (probes[-1].sent - probes[0].sent).total_seconds() if probes[0].sent and probes[-1].sent else 0
        out[0] += f", over {duration_text(span)}"
        for fate in (LOST_OUT, LOST_BACK, LOST_UNKNOWN):
            out.append(f"  {fate:<26} {fates[fate]:>5}  ({100 * fates[fate] / total:.2f}%)")
    out.append("")
    hosts = read_export(csv_path)
    for host, pings in hosts.items():
        out.append(report(summarise(host, pings, min_outage)))
        out.append("")
    answered = [p for p in probes if p.fate == ANSWERED]
    for name, values in (("outbound", [p.outbound_ms for p in answered]), ("inbound", [p.inbound_ms for p in answered])):
        zeros = sum(1 for v in values if v == 0)
        if values and zeros >= max(3, len(values) // 20):
            out.append(f"Note: {zeros} of {len(values)} {name} delays are exactly 0. twampy shows anything below zero as zero, so this "
                       f"points to the two machines' clocks disagreeing. Treat the {name} and its opposite as shifted by that difference; "
                       "the round trip, the jitter and the losses are unaffected.")
    return "\n".join(out).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run twampy and report full statistics for each direction.")
    parser.add_argument("far_end", nargs="?", help="the machine running twampy's responder, as IP:port")
    parser.add_argument("--near", default=":20000", help="local address and port to send from (default :20000)")
    parser.add_argument("--count", type=int, default=100, help="probes to send, up to 9999 (default 100)")
    parser.add_argument("--interval", type=int, default=100, help="milliseconds between probes, 100 to 1000 (default 100)")
    parser.add_argument("--twampy-src", type=Path, default=DEFAULT_TWAMPY_SRC, help="twampy's src folder")
    parser.add_argument("--from-log", help="read this verbose twampy sender log instead of running twampy")
    parser.add_argument("--csv", help="where to save the probes (default: twampy <time>.csv in the current folder)")
    parser.add_argument("--min-outage", type=int, default=2, help="lost probes in a row that count as an outage (default 2)")
    parser.add_argument("--plots", action="store_true", help="also draw a histogram for each direction and the spike picture")
    args = parser.parse_args(argv)

    if args.from_log:
        lines = Path(args.from_log).read_text(encoding="utf-8", errors="replace").splitlines()
    elif args.far_end:
        lines = run_twampy(args.far_end, args.near, args.count, args.interval, args.twampy_src)
    else:
        parser.error("give the far end as IP:port, or --from-log FILE")

    probes, far_end = parse_log(lines)
    if not probes:
        print("No probes were found in twampy's output. Is the responder running, and is the port open?", file=sys.stderr)
        return 1
    csv_path = Path(args.csv) if args.csv else Path(f"twampy {datetime.now():%Y-%m-%d %H-%M-%S}.csv")
    write_csv(probes, far_end or (args.far_end or "far end"), csv_path)
    print()
    print(statistics(probes, far_end or (args.far_end or "far end"), csv_path, args.min_outage))
    print(f"\nsaved {csv_path}")

    if args.plots:
        try:
            import matplotlib
            matplotlib.use("Agg")
        except ImportError:
            print("--plots needs matplotlib.", file=sys.stderr)
            return 2
        import ping_histogram
        import ping_spikes
        from ping_export import choose_times
        hosts = read_export(csv_path)
        for host, pings in hosts.items():
            picture = csv_path.with_name(f"{csv_path.stem} - {ping_histogram.safe_name(host)}.png")
            if ping_histogram.plot_host(summarise(host, pings, args.min_outage), pings, choose_times(pings), picture):
                print(f"picture: {picture}")
        per_host = ping_spikes.analyse(hosts)
        picture = csv_path.with_name(f"{csv_path.stem} - spikes.png")
        if ping_spikes.plot_spikes(hosts, per_host, picture):
            print(f"picture: {picture}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
