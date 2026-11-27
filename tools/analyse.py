#!/usr/bin/env python3
"""Analyses and plots any result file from the link-testing tools.

Give it a file and it works out which tool made it:

    PingTracer CSV export        report, a histogram per host, the spike picture, the deeper analysis
    irtt result (.json)          the same, for the way out, the way back and the round trip
    Crusader result (.crr/.json) the same
    twampy verbose log (.log)    the same

    py -3.15 analyse.py "results/irtt 2026-11-27 22-40-00.json"

The pictures and a CSV are saved beside the file.  Each kind is handled by its
own script (ping_report.py, ping_histogram.py, ping_spikes.py, irtt_stats.py,
crusader_stats.py, twampy_stats.py); this only chooses between them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def kind_of(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".crr":
        return "crusader"
    if suffix == ".csv":
        return "csv"
    if suffix == ".json":
        content = json.loads(path.read_text(encoding="utf-8"))
        if "round_trips" in content:
            return "irtt"
        if "pings" in content and "Crusader" in str(content.get("generated_by", "")):
            return "crusader"
        raise SystemExit(f"{path.name}: a JSON file, but not an irtt or Crusader result.")
    text = path.read_text(encoding="utf-8", errors="replace")
    if "sseq=" in text:
        return "twampy"
    raise SystemExit(f"{path.name}: not a file from PingTracer, irtt, Crusader or twampy that this recognises.")


def analyse(path: Path, extra: list[str]) -> int:
    kind = kind_of(path)
    print(f"{path.name}: {kind} result\n")
    import ping_deep
    saved = path.with_suffix(".csv")
    if kind == "irtt":
        import irtt_stats
        code = irtt_stats.main(["--from-json", str(path), "--csv", str(saved), "--plots", *extra])
    elif kind == "crusader":
        import crusader_stats
        code = crusader_stats.main([str(path), "--plots", *extra])
    elif kind == "twampy":
        import twampy_stats
        code = twampy_stats.main(["--from-log", str(path), "--csv", str(saved), "--plots", *extra])
    else:
        import ping_histogram
        import ping_report
        import ping_spikes
        code = 0
        for script in (ping_report, ping_histogram, ping_spikes):
            code = code or script.main([str(path)])
    if code:
        return code
    print()
    print("Deeper analysis (rhythm, loss bursts, baseline steps, stability, delay levels):")
    print()
    return ping_deep.main([str(saved)])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 1
    path = Path(argv[0])
    if not path.exists():
        print(f"{path} was not found.", file=sys.stderr)
        return 1
    return analyse(path, argv[1:])


if __name__ == "__main__":
    sys.exit(main())
