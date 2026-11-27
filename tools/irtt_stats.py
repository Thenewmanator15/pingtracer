#!/usr/bin/env python3
"""Runs irtt, or reads a result it saved, and gives statistics for each direction.

irtt (https://github.com/heistp/irtt) measures the way out and the way back
separately and saves every probe as JSON.  This turns that into the same
report and the same CSV as twampy_stats.py: the way out, the way back and the
round trip as three "hosts", so ping_histogram.py, ping_spikes.py and the
notebook work on it as they are.

On the far machine:

    irtt server

On this machine:

    py -3.15 irtt_stats.py 192.168.1.233 --seconds 60 --interval 100 --plots

To analyse a result irtt already saved (irtt client -o run.json ...):

    py -3.15 irtt_stats.py --from-json run.json --csv run.csv

Things to keep in mind:

* irtt says for every lost probe which direction lost it, so nothing has to be
  worked out here.
* irtt does not hide a clock difference.  If the two machines' clocks disagree,
  one direction reads too long and the other too short by that amount, and one
  can go below zero.  This script says so when it sees it.  The round trip,
  the jitter and the losses are not affected.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ping_export import percentile
from twampy_stats import ANSWERED, LOST_BACK, LOST_OUT, LOST_UNKNOWN, Probe, statistics, write_csv

def _first_existing(*candidates: Path) -> Path:
    """The tool beside the scripts (the link-test-tools folder layout) if it is there, else in dist/third-party."""
    return next((c for c in candidates if c.exists()), candidates[-1])


_HERE = Path(__file__).resolve().parent
DEFAULT_IRTT = _first_existing(_HERE.parent / "irtt" / "irtt.exe", _HERE.parent / "dist" / "third-party" / "irtt" / "irtt.exe")
FATES = {"false": ANSWERED, "true_up": LOST_OUT, "true_down": LOST_BACK, "true": LOST_UNKNOWN}


def read_result(result: dict) -> tuple[list[Probe], str]:
    """Turns irtt's saved result into probes.  Returns them in order, and the far end's address."""
    probes = []
    for trip in result["round_trips"]:
        probe = Probe(trip["seqno"], fate=FATES.get(str(trip["lost"]).lower(), LOST_UNKNOWN))
        sent = trip["timestamps"]["client"].get("send", {}).get("wall")
        if sent:
            probe.sent = datetime.fromtimestamp(sent / 1e9)
        if probe.fate == ANSWERED:
            delay = trip["delay"]
            probe.round_trip_ms = delay["rtt"] / 1e6
            # Without a wall clock at both ends irtt gives the round trip only.
            if "send" not in delay or "receive" not in delay:
                raise SystemExit("This result has no delay for each direction. Run irtt with its default clock setting (--clock=both).")
            probe.outbound_ms = delay["send"] / 1e6
            probe.inbound_ms = delay["receive"] / 1e6
        probes.append(probe)
    far_end = result.get("config", {}).get("remote_address", "") or "far end"
    return probes, far_end


def run_irtt(far_end: str, seconds: float, interval_ms: float, irtt: Path, json_path: Path) -> dict:
    """Runs irtt's client, saving its result to json_path, and returns that result."""
    if not Path(irtt).exists():
        raise SystemExit(f"irtt was not found at {irtt}. Give its path with --irtt.")
    command = [str(irtt), "client", "-Q", "-i", f"{interval_ms:g}ms", "-d", f"{seconds:g}s", "-o", str(json_path), far_end]
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0 or not json_path.exists():
        raise SystemExit(f"irtt did not finish: {(done.stderr or done.stdout).strip()}")
    return json.loads(json_path.read_text(encoding="utf-8"))


def clock_note(probes: list[Probe]) -> str:
    answered = [p for p in probes if p.fate == ANSWERED]
    for name, values in (("way out", [p.outbound_ms for p in answered]), ("way back", [p.inbound_ms for p in answered])):
        if values and percentile(values, 0.5) <= 0:
            return (f"Note: the {name} reads zero or less. The two machines' clocks disagree, so the way out and the way back are "
                    "shifted by that difference in opposite directions. The round trip, the jitter and the losses are unaffected.")
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run irtt and report full statistics for each direction.")
    parser.add_argument("far_end", nargs="?", help="the machine running 'irtt server', as IP or IP:port")
    parser.add_argument("--seconds", type=float, default=30, help="how long to test (default 30)")
    parser.add_argument("--interval", type=float, default=100, help="milliseconds between probes (default 100)")
    parser.add_argument("--irtt", type=Path, default=DEFAULT_IRTT, help="path to the irtt program")
    parser.add_argument("--from-json", help="read this saved irtt result instead of running irtt")
    parser.add_argument("--csv", help="where to save the probes (default: irtt <time>.csv in the current folder)")
    parser.add_argument("--min-outage", type=int, default=2, help="lost probes in a row that count as an outage (default 2)")
    parser.add_argument("--plots", action="store_true", help="also draw a histogram for each direction and the spike picture")
    args = parser.parse_args(argv)

    csv_path = Path(args.csv) if args.csv else Path(f"irtt {datetime.now():%Y-%m-%d %H-%M-%S}.csv")
    if args.from_json:
        result = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    elif args.far_end:
        result = run_irtt(args.far_end, args.seconds, args.interval, args.irtt, csv_path.with_suffix(".json"))
    else:
        parser.error("give the far end as IP or IP:port, or --from-json FILE")

    probes, far_end = read_result(result)
    if not probes:
        print("irtt's result holds no probes.", file=sys.stderr)
        return 1
    write_csv(probes, far_end, csv_path)
    print(statistics(probes, far_end, csv_path, args.min_outage))
    totals = result.get("stats", {})
    if "late_packets" in totals or "duplicates" in totals:
        print()
        print(f"Out of order: {totals.get('late_packets', 0)} reply/replies arrived after a later one "
              f"({totals.get('late_packets_percent', 0):.2f}%).  Duplicates: {totals.get('duplicates', 0)} ({totals.get('duplicate_percent', 0):.2f}%).")
    note = clock_note(probes)
    if note:
        print("\n" + note)
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
        from ping_export import choose_times, read_export, summarise
        hosts = read_export(csv_path)
        for host, pings in hosts.items():
            picture = csv_path.with_name(f"{csv_path.stem} - {ping_histogram.safe_name(host)}.png")
            if ping_histogram.plot_host(summarise(host, pings, args.min_outage), pings, choose_times(pings), picture):
                print(f"picture: {picture}")
        picture = csv_path.with_name(f"{csv_path.stem} - spikes.png")
        if ping_spikes.plot_spikes(hosts, ping_spikes.analyse(hosts), picture):
            print(f"picture: {picture}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
