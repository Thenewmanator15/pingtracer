#!/usr/bin/env python3
"""Reads a Crusader result and gives latency statistics for each direction.

Crusader (https://github.com/Zoxc/crusader) saves each test as a .crr file and
draws its own picture.  This reads the latency probes out of that file and
gives the same report, CSV and pictures as irtt_stats.py and twampy_stats.py:
the way up (outbound), the way down (inbound) and the round trip as three
"hosts".

    py -3.15 crusader_stats.py "crusader-results/test 2026-11-27 22.15.00.crr" --plots

A .crr file is converted with Crusader's own "export" command, so the Crusader
program is needed for those; a JSON file it already exported is read directly.

Things to keep in mind:

* Crusader works out the difference between the two machines' clocks itself
  before splitting the round trip into up and down.  How well it does that has
  not been checked here.
* The file holds times since the start of the test, not the time of day, so
  the times shown count from when the file was saved minus the test's length.
* If the test also loaded the link, the probes cover the loaded and the idle
  parts together.  Crusader's own picture shows which is which.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from twampy_stats import ANSWERED, LOST_BACK, LOST_UNKNOWN, Probe, statistics, write_csv

def _first_existing(*candidates: Path) -> Path:
    """The tool beside the scripts (the link-test-tools folder layout) if it is there, else in dist/third-party."""
    return next((c for c in candidates if c.exists()), candidates[-1])


_HERE = Path(__file__).resolve().parent
DEFAULT_CRUSADER = _first_existing(_HERE.parent / "crusader" / "crusader.exe",
                                   _HERE.parent / "dist" / "third-party" / "crusader" / "Crusader-Windows-X86-64-bit" / "crusader.exe")


def seconds(duration: dict | None) -> float | None:
    return None if duration is None else duration["secs"] + duration["nanos"] / 1e9


def read_result(result: dict, ended: datetime | None = None, far_end: str = "far end") -> tuple[list[Probe], str]:
    """Turns Crusader's exported result into probes, in the order sent."""
    pings = sorted(result.get("pings") or [], key=lambda p: p["index"])
    ended = ended or datetime.now()
    length = seconds(result.get("duration")) or 0.0
    began = ended - timedelta(seconds=(seconds(result.get("start")) or 0.0) + length)
    probes = []
    for ping in pings:
        probe = Probe(ping["index"], sent=began + timedelta(seconds=seconds(ping["sent"])))
        latency = ping.get("latency") or {}
        total, up = seconds(latency.get("total")), seconds(latency.get("up"))
        if total is not None and up is not None:
            probe.fate = ANSWERED
            probe.round_trip_ms = total * 1000
            probe.outbound_ms = up * 1000
            probe.inbound_ms = (total - up) * 1000
        elif up is not None:
            probe.fate = LOST_BACK       # it reached the far end; the reply did not come back
        else:
            probe.fate = LOST_UNKNOWN
        probes.append(probe)
    return probes, far_end


def load(path: Path, crusader: Path) -> dict:
    """Reads a .json export directly, or converts a .crr with Crusader first."""
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if not Path(crusader).exists():
        raise SystemExit(f"Crusader was not found at {crusader}. Give its path with --crusader, or export the file to JSON first.")
    with tempfile.TemporaryDirectory() as folder:
        exported = Path(folder) / "export.json"
        done = subprocess.run([str(crusader), "export", "-f", "-o", str(exported), str(path)], capture_output=True, text=True)
        if done.returncode != 0 or not exported.exists():
            raise SystemExit(f"Crusader could not export {path}: {(done.stderr or done.stdout).strip()}")
        return json.loads(exported.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report latency statistics for each direction from a Crusader result.")
    parser.add_argument("result", help="a .crr file Crusader saved, or a JSON file from 'crusader export'")
    parser.add_argument("--crusader", type=Path, default=DEFAULT_CRUSADER, help="path to the crusader program (for .crr files)")
    parser.add_argument("--name", default="far end", help="what to call the far end in the report")
    parser.add_argument("--csv", help="where to save the probes (default: beside the result)")
    parser.add_argument("--min-outage", type=int, default=2, help="lost probes in a row that count as an outage (default 2)")
    parser.add_argument("--plots", action="store_true", help="also draw a histogram for each direction and the spike picture")
    args = parser.parse_args(argv)

    path = Path(args.result)
    result = load(path, args.crusader)
    probes, far_end = read_result(result, datetime.fromtimestamp(path.stat().st_mtime), args.name)
    if not probes:
        print("This Crusader result holds no latency probes.", file=sys.stderr)
        return 1
    csv_path = Path(args.csv) if args.csv else path.with_suffix(".csv")
    write_csv(probes, far_end, csv_path)
    print(statistics(probes, far_end, csv_path, args.min_outage))
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
