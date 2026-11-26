"""Reads a PingTracer CSV export and works out the statistics.

This is the shared part: it has no command line of its own.  The scripts that
use it are:

    ping_report.py      prints the numbers, drop-outs and altered replies
    ping_histogram.py   draws a histogram of ping times for each host
    test_ping_tools.py  checks all of this against known data and NumPy

Only the standard library is needed here.

The numbers are worked out the same way PingTracer works out its own, so the
two can be compared:

* baseline  = the shortest ping time (the path with nothing queued)
* worst 1%  = the smallest time that at least 99% of pings were at or below
* jitter    = the mean change between one ping and the next; a lost ping
              breaks the pairing
* loss      = pings with a result that was not a success; pings still waiting
              for a reply are never written to the export

PingTracer's on-screen figures cover only the pings visible in its window.
These cover every ping in the file.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")
SUCCESS_RESULTS = ("Success", "DataMismatch")


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


# ------------------------------------------------------------------------ reading


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


def choose_times(pings: list[Ping], use_measured: bool = False) -> list[float | None]:
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
            kind = ping.result or "(blank)"
            summary.failure_kinds[kind] = summary.failure_kinds.get(kind, 0) + 1
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


# ----------------------------------------------------------------------- wording


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
