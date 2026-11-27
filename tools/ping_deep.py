#!/usr/bin/env python3
"""Deeper analysis of a ping record, aimed at links that change over time.

Works on a PingTracer CSV export, and on the CSVs that irtt_stats.py,
twampy_stats.py and crusader_stats.py save.  For every host it measures five
things and gives a number for how sure each one is:

  1. Rhythm      Is there a repeating pattern in the delay, or in the lost and
                 slow pings?  (Lomb-Scargle periodogram; the chance of a peak
                 that high in shuffled data is given.)  A link that switches
                 satellite on a schedule would show one.
  2. Loss bursts Do lost pings come in clumps, or one at a time as if by coin
                 toss?  (Two-state Gilbert model and a runs test.)
  3. Steps       Did the fastest achievable delay jump to a new level, and
                 when?  (Change points in the baseline.)  A change of path
                 length shows up this way.
  4. Stability   Over what averaging time is the delay steadiest, and does it
                 wander beyond that?  (Overlapping Allan deviation.)
  5. Modes       Does the delay sit at one level or at several?  (Peaks in
                 the smoothed distribution, with the share of pings at each.)

    py -3.15 ping_deep.py export.csv
    py -3.15 ping_deep.py export.csv --out plots --max-period 120

Needs NumPy, and Matplotlib for the pictures.  test_ping_deep.py checks every
part on made-up data with known answers, and against Astropy and AllanTools
where those are installed.

These are measurements of the record, not explanations.  A rhythm at 15 s says
something repeats every 15 s; it does not say what.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ping_export import Ping, choose_times, duration_text, ms, read_export

SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SERIES, CRITICAL = "#e1e0d9", "#c3c2b7", "#2a78d6", "#d03b3b"
SURE = 0.01   # a chance below this is called "found"; above it, "not found"


# ---------------------------------------------------------------- 1. rhythm

@dataclass
class Rhythm:
    what: str                     # "delay" or "lost and slow pings"
    tested: bool = False
    reason: str = ""              # why not tested
    period: float = 0.0           # seconds: the repeat time (the longest one whose peak passes the test)
    strongest: float = 0.0        # seconds, of the strongest peak; a short bump shows strongest at period / 2, / 3 ...
    power: float = 0.0            # 0 to 1: share of the variation that peak explains
    stands_out: float = 0.0       # that peak, as a multiple of the typical power near it
    chance: float = 1.0           # chance of a peak standing out this much in shuffled data
    periods: np.ndarray = field(default_factory=lambda: np.empty(0))
    powers: np.ndarray = field(default_factory=lambda: np.empty(0))
    threshold: np.ndarray = field(default_factory=lambda: np.empty(0))   # power that 1% of shuffles reach, at each period

    @property
    def found(self) -> bool:
        return self.tested and self.chance < SURE


def lomb_scargle(t: np.ndarray, y: np.ndarray, frequencies: np.ndarray, shuffles: np.ndarray | None = None, chunk: int = 256):
    """Classic Lomb-Scargle power (0 to 1) of y, with its mean removed, at each frequency.

    If `shuffles` (n by K, each column a shuffled copy of y) is given, also returns the power of every
    shuffle at every frequency (frequencies by K).
    """
    y = y - y.mean()
    total = float(y @ y)
    powers = np.empty(len(frequencies))
    highest = None
    if shuffles is not None:
        shuffles = shuffles - shuffles.mean(axis=0)
        totals = np.einsum("ij,ij->j", shuffles, shuffles)
        highest = np.empty((len(frequencies), shuffles.shape[1]))
    for start in range(0, len(frequencies), chunk):
        omega = 2 * np.pi * frequencies[start:start + chunk][:, None]
        tau = np.arctan2(np.sin(2 * omega * t).sum(axis=1), np.cos(2 * omega * t).sum(axis=1))[:, None] / (2 * omega)
        c, s = np.cos(omega * (t - tau)), np.sin(omega * (t - tau))
        cc, ss = (c * c).sum(axis=1), (s * s).sum(axis=1)
        ss = np.where(ss < 1e-12, np.inf, ss)
        powers[start:start + chunk] = ((c @ y) ** 2 / cc + (s @ y) ** 2 / ss) / total
        if shuffles is not None:
            highest[start:start + chunk] = ((c @ shuffles) ** 2 / cc[:, None] + (s @ shuffles) ** 2 / ss[:, None]) / totals
    return (powers, highest) if shuffles is not None else powers


def background(powers: np.ndarray, width: int) -> np.ndarray:
    """The typical (median) power in each stretch of `width` neighbouring frequencies, same shape as powers.

    Delay that stays high for a few pings at a time puts extra power across a broad band of slow repeat
    times without any true rhythm.  Dividing by the local background removes that; a real rhythm is a
    narrow peak and still stands out.
    """
    result = np.empty_like(powers)
    for start in range(0, len(powers), width):
        stop = min(start + width, len(powers))
        if stop - start < width // 2 and start:          # a short last stretch joins the one before
            start = start - width
        result[start:stop] = np.median(powers[start:stop], axis=0)
    return np.maximum(result, 1e-300)


def remove_slow_trend(t: np.ndarray, y: np.ndarray, window: float) -> np.ndarray:
    """Subtracts the median of each `window`-second stretch, joined by straight lines.

    Slow drift and steps would otherwise look like a "rhythm" as long as the whole record.
    """
    edges = np.arange(t[0], t[-1] + window, window)
    if len(edges) < 3:
        return y - np.median(y)
    cuts = np.searchsorted(t, edges)                      # t is in order, so each stretch is one slice
    cuts[-1] = len(t)
    centres, medians = [], []
    for block in range(len(edges) - 1):
        inside = y[cuts[block]:cuts[block + 1]]
        if len(inside):
            centres.append((edges[block] + edges[block + 1]) / 2)
            medians.append(np.median(inside))
    return y - np.interp(t, centres, medians)


def folded_share(t: np.ndarray, y: np.ndarray, period: float, bins: int = 32) -> float:
    """Share of the variation in y explained by its average shape over one `period` (adjusted for chance)."""
    which = np.minimum(((t % period) / period * bins).astype(int), bins - 1)
    counts = np.bincount(which, minlength=bins)
    means = np.bincount(which, weights=y, minlength=bins) / np.maximum(counts, 1)
    centred = y - y.mean()
    left = float(((y - means[which]) ** 2).sum()) / max(len(y) - bins, 1)
    return 1 - left / (float(centred @ centred) / (len(y) - 1))


FFT_ABOVE = 20000    # records longer than this many replies use the fast Fourier transform


def spectrum_by_fft(t: np.ndarray, y: np.ndarray, interval: float, low: float, high: float, shuffles: int, rng):
    """The same periodogram for a long record, by fast Fourier transform.

    The pings are put on an even grid one ping interval apart (a lost ping leaves a gap at the average),
    which is what makes the fast method possible; for evenly sent pings it gives the Lomb-Scargle result.
    Each shuffle moves the values among the places that had a reply, so the gaps stay where they were.
    Returns the frequencies, the power at each, the local background, and each shuffle's highest
    power-over-background.
    """
    y = y - y.mean()
    cell = np.round((t - t[0]) / interval).astype(np.int64)
    size = int(cell[-1]) + 1
    counts = np.bincount(cell, minlength=size)
    occupied = counts > 0
    padded = 1 << int(math.ceil(math.log2(2 * size)))      # twice the length: two points per natural peak width
    every = np.fft.rfftfreq(padded, interval)
    first, last = np.searchsorted(every, [1 / high, 1 / low])
    frequencies = every[first:last + 1]
    width = max(40, len(frequencies) // 25)

    def power_of(values: np.ndarray) -> np.ndarray:
        grid = np.bincount(cell, weights=values, minlength=size)
        grid[occupied] /= counts[occupied]
        found = np.fft.rfft(grid, padded)[first:last + 1]
        return 2 * (found.real ** 2 + found.imag ** 2) / (int(occupied.sum()) * float(grid @ grid))

    powers = power_of(y)
    level = background(powers, width)
    shuffled_ratio = np.empty(shuffles)
    for k in range(shuffles):
        shuffled = power_of(rng.permutation(y))
        shuffled_ratio[k] = (shuffled / background(shuffled, width)).max()
    return frequencies, powers, level, shuffled_ratio


def find_rhythm(what: str, t: np.ndarray, y: np.ndarray, interval: float, min_period: float | None = None,
                max_period: float | None = None, shuffles: int = 200, seed: int = 1) -> Rhythm:
    result = Rhythm(what)
    span = float(t[-1] - t[0]) if len(t) else 0.0
    low = max(min_period or 0.0, 2.5 * interval, 1.0)
    high = min(max_period or 300.0, span / 8)
    if len(t) < 100 or high <= low * 2:
        result.reason = "record too short"
        return result
    y = remove_slow_trend(t, y.astype(float), high)
    if np.ptp(y) == 0:
        result.reason = "no variation"
        return result
    rng = np.random.default_rng(seed)
    if len(t) > FFT_ABOVE:
        frequencies, powers, level, shuffled_ratio = spectrum_by_fft(t, y, interval, low, high, shuffles, rng)
    else:
        # Evenly spaced in frequency, four points per natural peak width; capped so the sum stays quick.
        count = int(min(max(4 * span * (1 / low - 1 / high), 50), 6000))
        frequencies = np.linspace(1 / high, 1 / low, count)
        shuffled = np.stack([rng.permutation(y) for _ in range(shuffles)], axis=1)
        powers, shuffled_powers = lomb_scargle(t, y, frequencies, shuffled)
        width = max(40, count // 25)
        level = background(powers, width)
        shuffled_ratio = (shuffled_powers / background(shuffled_powers, width)).max(axis=0)
    ratio = powers / level
    best = int(ratio.argmax())
    result.tested = True
    result.strongest = result.period = 1 / frequencies[best]
    result.power, result.stands_out = float(powers[best]), float(ratio[best])
    # A brief bump that repeats every T seconds is strongest at T/2, T/3 ... as often as at T itself.  To get
    # T, fold the record at 1, 2, 3 ... times the strongest repeat time and see how much of the variation
    # each fold explains; T is the shortest that explains nearly as much as the best.
    spacing = frequencies[1] - frequencies[0]
    explained = {}
    for multiple in range(1, 41):
        centre = result.strongest * multiple
        if centre > high:
            break
        reach = 1.5 * centre * centre * spacing              # how far off the grid can have left it
        explained[multiple] = max((folded_share(t, y, trial), trial) for trial in np.linspace(centre - reach, centre + reach, 41))
    most = max(share for share, _ in explained.values())
    result.period = next(period for share, period in explained.values() if share >= 0.8 * most) if most > 0 else result.strongest
    result.chance = (1 + int((shuffled_ratio >= ratio[best]).sum())) / (shuffles + 1)
    threshold = level * float(np.quantile(shuffled_ratio, 0.99))
    if len(frequencies) > 20000:                           # keep the tallest of each group, enough for a picture
        group = -(-len(frequencies) // 20000)
        starts = np.arange(0, len(frequencies), group)
        tallest = starts + np.array([int(powers[s:s + group].argmax()) for s in starts])
        frequencies, powers, threshold = frequencies[tallest], powers[tallest], threshold[tallest]
    result.periods, result.powers, result.threshold = 1 / frequencies, powers, threshold
    if result.chance >= SURE:
        result.period = result.strongest
    return result


# ----------------------------------------------------------- 2. loss bursts

@dataclass
class LossBursts:
    pings: int = 0
    lost: int = 0
    tested: bool = False
    after_ok: float = 0.0         # chance a ping is lost when the one before arrived
    after_lost: float = 0.0       # chance a ping is lost when the one before was lost too
    mean_burst: float = 0.0
    longest: int = 0
    lengths: dict[int, int] = field(default_factory=dict)   # burst length -> how many
    runs_z: float = 0.0
    chance: float = 1.0           # chance of this few runs if losses were independent

    @property
    def rate(self) -> float:
        return self.lost / self.pings if self.pings else 0.0

    @property
    def bursty(self) -> bool:
        return self.tested and self.chance < SURE and self.runs_z < 0


def loss_bursts(lost: np.ndarray, min_lost: int = 5) -> LossBursts:
    lost = np.asarray(lost, dtype=bool)
    result = LossBursts(len(lost), int(lost.sum()))
    if result.lost < min_lost or result.lost == len(lost):
        return result
    before, after = lost[:-1], lost[1:]
    result.after_ok = float((~before & after).sum() / max((~before).sum(), 1))
    result.after_lost = float((before & after).sum() / max(before.sum(), 1))
    edges = np.flatnonzero(np.diff(np.concatenate(([False], lost, [False])).astype(int)))
    lengths = edges[1::2] - edges[::2]
    result.mean_burst, result.longest = float(lengths.mean()), int(lengths.max())
    values, counts = np.unique(lengths, return_counts=True)
    result.lengths = {int(v): int(c) for v, c in zip(values, counts)}
    # Wald-Wolfowitz runs test: clumped losses make fewer runs than independent ones would.
    n1, n0, n = result.lost, len(lost) - result.lost, len(lost)
    runs = 1 + int((before != after).sum())
    expected = 2 * n0 * n1 / n + 1
    variance = 2 * n0 * n1 * (2 * n0 * n1 - n) / (n * n * (n - 1))
    if variance > 0:
        result.runs_z = (runs - expected) / math.sqrt(variance)
        result.chance = math.erfc(abs(result.runs_z) / math.sqrt(2))
        result.tested = True
    return result


# ----------------------------------------------------------------- 3. steps

@dataclass
class Step:
    seconds: float                # since the start of the record
    before: float
    after: float

    @property
    def size(self) -> float:
        return self.after - self.before


@dataclass
class Baseline:
    block_seconds: float = 0.0
    times: np.ndarray = field(default_factory=lambda: np.empty(0))
    floor: np.ndarray = field(default_factory=lambda: np.empty(0))    # fastest ping in each block
    noise: float = 0.0
    resolution: float = 0.0       # the data's own step size when it is coarse (1 for whole milliseconds)
    steps: list[Step] = field(default_factory=list)
    levels: list[tuple[float, float, float]] = field(default_factory=list)   # start, end, level


def baseline_steps(t: np.ndarray, y: np.ndarray, interval: float, block_seconds: float | None = None,
                   min_step_ms: float = 0.5, min_blocks: int = 5) -> Baseline:
    """Finds lasting jumps in the fastest delay.

    The record is cut into short blocks and the fastest ping of each is kept: queueing only ever adds
    delay, so the fastest ping follows the path itself.  The series of block minimums is then split
    wherever a split lowers the squared error by more than chance would (binary segmentation).
    """
    result = Baseline(block_seconds or max(10 * interval, 2.0))
    if len(t) < 50:
        return result
    which = ((t - t[0]) // result.block_seconds).astype(int)
    starts = np.concatenate(([0], np.flatnonzero(np.diff(which)) + 1))     # t is in order: each block is one slice
    blocks = which[starts]
    floor = np.minimum.reduceat(y, starts)
    times = (blocks + 0.5) * result.block_seconds
    result.times, result.floor = times, floor
    if len(floor) < 2 * min_blocks:
        result.levels = [(0.0, float(t[-1] - t[0]), float(np.median(floor)))]
        return result
    # Whole-millisecond data (Windows ping) flips between neighbouring whole numbers; that is rounding, not a step.
    distinct = np.unique(y)
    resolution = float(np.diff(distinct).min()) if len(distinct) > 1 else 0.0
    resolution = resolution if resolution >= 0.5 else 0.0
    result.resolution = resolution
    scatter = 1.4826 * np.median(np.abs(np.diff(floor) - np.median(np.diff(floor)))) / math.sqrt(2)
    result.noise = float(max(scatter, resolution / math.sqrt(12), 1e-3))
    penalty = 3 * result.noise ** 2 * math.log(len(floor))
    smallest = max(min_step_ms, 3 * result.noise, 1.5 * resolution)

    def split(lo: int, hi: int) -> list[int]:
        m = hi - lo
        if m < 2 * min_blocks:
            return []
        segment = floor[lo:hi]
        sums, squares = np.cumsum(segment), np.cumsum(segment ** 2)
        k = np.arange(min_blocks, m - min_blocks + 1)
        left = squares[k - 1] - sums[k - 1] ** 2 / k
        right = (squares[-1] - squares[k - 1]) - (sums[-1] - sums[k - 1]) ** 2 / (m - k)
        whole = squares[-1] - sums[-1] ** 2 / m
        best = int((left + right).argmin())
        cut = lo + int(k[best])
        jump = abs(np.median(floor[cut:hi]) - np.median(floor[lo:cut]))
        if whole - (left + right)[best] <= penalty or jump < smallest:
            return []
        return split(lo, cut) + [cut] + split(cut, hi)

    cuts = [0] + split(0, len(floor)) + [len(floor)]
    for a, b in zip(cuts[:-1], cuts[1:]):
        result.levels.append((float(blocks[a] * result.block_seconds), float((blocks[b - 1] + 1) * result.block_seconds), float(np.median(floor[a:b]))))
    for (_, end, before), (_, _, after) in zip(result.levels[:-1], result.levels[1:]):
        result.steps.append(Step(end, before, after))
    return result


# ------------------------------------------------------------- 4. stability

@dataclass
class Stability:
    tested: bool = False
    taus: np.ndarray = field(default_factory=lambda: np.empty(0))       # averaging time, seconds
    deviation: np.ndarray = field(default_factory=lambda: np.empty(0))  # Allan deviation, ms
    best_tau: float = 0.0
    best_deviation: float = 0.0
    short_slope: float = 0.0      # log-log slope over the shortest averaging times
    long_slope: float = 0.0       # the same over the longest
    filled: int = 0               # lost pings filled in by straight lines


def allan_deviation(y: np.ndarray, rate: float, factors: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Overlapping Allan deviation of evenly sampled values y (taken `rate` times a second).

    Returns the averaging times and the deviation at each, in the units of y.  It answers: if the delay
    is averaged over this long, how much does one average differ from the next?
    """
    x = np.concatenate(([0.0], np.cumsum(y - y.mean()))) / rate
    n = len(x)
    if factors is None:
        factors = np.unique(np.round(2 ** np.arange(0, math.log2(max((n - 1) // 4, 1)) + 1e-9, 0.5)).astype(int))
    taus, deviations = [], []
    for m in factors:
        if n - 2 * m < 2:
            break
        second = x[2 * m:] - 2 * x[m:n - m] + x[:n - 2 * m]
        tau = m / rate
        taus.append(tau)
        deviations.append(math.sqrt(float(second @ second) / (2 * tau * tau * (n - 2 * m))))
    return np.array(taus), np.array(deviations)


def stability(values: list[float | None], interval: float) -> Stability:
    result = Stability()
    have = np.array([v is not None for v in values])
    if have.sum() < 200 or interval <= 0:
        return result
    index = np.arange(len(values))
    y = np.interp(index, index[have], np.array([v for v in values if v is not None], dtype=float))
    result.filled = int((~have).sum())
    taus, deviation = allan_deviation(y, 1 / interval)
    if len(taus) < 4 or not np.all(deviation > 0):
        return result
    result.tested, result.taus, result.deviation = True, taus, deviation
    best = int(deviation.argmin())
    result.best_tau, result.best_deviation = float(taus[best]), float(deviation[best])
    third = max(len(taus) // 3, 2)
    result.short_slope = float(np.polyfit(np.log(taus[:third]), np.log(deviation[:third]), 1)[0])
    result.long_slope = float(np.polyfit(np.log(taus[-third:]), np.log(deviation[-third:]), 1)[0])
    return result


# ----------------------------------------------------------------- 5. modes

@dataclass
class Mode:
    centre: float
    share: float                  # of the replies, 0 to 1
    low: float
    high: float


@dataclass
class Modes:
    grid: np.ndarray = field(default_factory=lambda: np.empty(0))
    density: np.ndarray = field(default_factory=lambda: np.empty(0))
    modes: list[Mode] = field(default_factory=list)
    width: float = 0.0            # smoothing width used, ms


def delay_modes(y: np.ndarray, min_share: float = 0.03, valley: float = 0.5) -> Modes:
    """Finds the levels the delay sits at: peaks of the smoothed distribution.

    Two peaks count as separate only if the dip between them falls below `valley` times the lower peak,
    and a peak must hold at least `min_share` of the replies.
    """
    result = Modes()
    if len(y) < 50:
        return result
    low, high = np.quantile(y, [0.002, 0.995])
    core = y[(y >= low) & (y <= high)]
    spread = min(core.std(), np.subtract(*np.quantile(core, [0.75, 0.25])) / 1.34) or core.std() or 0.5
    width = 0.9 * spread * len(core) ** -0.2
    if np.all(np.mod(y, 1) == 0):
        width = max(width, 0.6)   # whole-millisecond data: do not resolve the gaps between whole numbers
    width = max(width, 1e-3)
    grid = np.linspace(low - 3 * width, high + 3 * width, 1024)
    counts, edges = np.histogram(core, bins=len(grid), range=(grid[0], grid[-1]))
    step = edges[1] - edges[0]
    reach = int(math.ceil(4 * width / step))
    kernel = np.exp(-0.5 * (np.arange(-reach, reach + 1) * step / width) ** 2)
    density = np.convolve(counts, kernel / kernel.sum(), mode="same") / (len(core) * step)
    centres = (edges[:-1] + edges[1:]) / 2
    result.grid, result.density, result.width = centres, density, float(width)

    peaks = [i for i in range(1, len(density) - 1) if density[i] > density[i - 1] and density[i] >= density[i + 1]
             and density[i] > 0.01 * density.max()]
    kept: list[int] = []
    for peak in peaks:                                   # merge peaks with no real dip between them
        if kept and density[kept[-1]:peak + 1].min() > valley * min(density[kept[-1]], density[peak]):
            if density[peak] > density[kept[-1]]:
                kept[-1] = peak
        else:
            kept.append(peak)
    while True:
        bounds = [-math.inf] + [float(centres[a + int(density[a:b + 1].argmin())]) for a, b in zip(kept[:-1], kept[1:])] + [math.inf]
        shares = [float(((y >= lo) & (y < hi)).mean()) for lo, hi in zip(bounds[:-1], bounds[1:])]
        small = [i for i, share in enumerate(shares) if share < min_share]
        if not small or len(kept) == 1:
            break
        kept.pop(min(small, key=lambda i: shares[i]))      # drop the smallest; its pings join a neighbour
    result.modes = [Mode(float(centres[p]), share, lo, hi) for p, share, lo, hi in zip(kept, shares, bounds[:-1], bounds[1:])]
    return result


# ------------------------------------------------------------- all together

@dataclass
class Deep:
    host: str
    pings: int
    replies: int
    span: float
    interval: float
    rhythm_delay: Rhythm
    rhythm_events: Rhythm
    loss: LossBursts
    baseline: Baseline
    stability: Stability
    modes: Modes
    slow_above: float = 0.0


def analyse_host(host: str, pings: list[Ping], times: list[float | None], max_period: float | None = None,
                 min_period: float | None = None, shuffles: int = 200) -> Deep:
    start = pings[0].sent
    sent = np.array([(p.sent - start).total_seconds() for p in pings])
    have = np.array([v is not None for v in times])
    t, y = sent[have], np.array([v for v in times if v is not None], dtype=float)
    interval = float(np.median(np.diff(sent))) if len(sent) > 1 else 0.0
    if len(y) < 50:
        empty = Rhythm("delay", reason="too few replies")
        return Deep(host, len(pings), len(y), float(sent[-1]) if len(sent) else 0.0, interval, empty,
                    Rhythm("lost and slow pings", reason="too few replies"), loss_bursts(~have), Baseline(), Stability(), Modes())
    # "Slow" here is the same rule ping_spikes.py uses: above the median by 3 IQR, at least 10 ms.
    q1, median, q3 = np.quantile(y, [0.25, 0.5, 0.75])
    slow_above = float(median + max(10.0, 3 * (q3 - q1)))
    events = (~have).astype(float)
    events[have] = (y > slow_above).astype(float)
    if events.sum() >= 5:
        rhythm_events = find_rhythm("lost and slow pings", sent, events, interval, min_period, max_period, shuffles)
    else:
        rhythm_events = Rhythm("lost and slow pings", reason=f"only {int(events.sum())} lost or slow ping(s)")
    return Deep(host, len(pings), len(y), float(sent[-1]), interval,
                find_rhythm("delay", t, y, interval, min_period, max_period, shuffles), rhythm_events,
                loss_bursts(~have), baseline_steps(t, y, interval), stability(times, interval), delay_modes(y), slow_above)


def chance_text(chance: float, shuffles: bool = False) -> str:
    if shuffles and chance <= 1 / 150:
        return "less than 1 in 200"
    if chance < 1e-6:
        return "less than 1 in a million"
    return f"1 in {round(1 / chance):,}" if chance < 0.1 else f"{chance:.2f}"


def rhythm_line(r: Rhythm) -> str:
    if not r.tested:
        return f"  rhythm in {r.what}: not tested ({r.reason})"
    verdict = "FOUND" if r.found else "none found"
    repeat = f"Repeats every {r.period:.1f} s (strongest peak at {r.strongest:.1f} s" if r.found and abs(r.period - r.strongest) > 0.02 * r.strongest else f"Strongest at {r.strongest:.1f} s ("
    repeat += ", " if repeat.endswith("s") else ""
    if r.found and not repeat.startswith("Repeats"):
        repeat = f"Repeats every {r.period:.1f} s ("
    return (f"  rhythm in {r.what}: {verdict}. {repeat}{r.stands_out:.0f} times its surroundings, "
            f"{100 * r.power:.1f}% of the variation); chance of that in shuffled data {chance_text(r.chance, True)}")


def report(d: Deep) -> str:
    out = [f"== {d.host}", f"  {d.pings} pings, {d.replies} replies, over {duration_text(d.span)}, one every {d.interval:.2f} s"]
    out += [rhythm_line(d.rhythm_delay), rhythm_line(d.rhythm_events)]
    l = d.loss
    if l.tested:
        verdict = "CLUMPED" if l.bursty else "no clumping found"
        out.append(f"  loss bursts: {verdict}. {l.lost} lost ({100 * l.rate:.2f}%). After a lost ping the next is lost "
                   f"{100 * l.after_lost:.0f}% of the time, after a good one {100 * l.after_ok:.2f}%. Mean burst {l.mean_burst:.2f} pings, "
                   f"longest {l.longest}; chance of this if losses were independent {chance_text(l.chance)}")
    else:
        out.append(f"  loss bursts: not tested ({l.lost} lost, too few)")
    b = d.baseline
    if not len(b.floor):
        out.append("  baseline steps: not tested (too few replies)")
    elif b.steps:
        out.append(f"  baseline steps: {len(b.steps)} FOUND (fastest ping in each {b.block_seconds:g} s block; block-to-block noise {b.noise:.2f} ms)")
        for s in b.steps:
            out.append(f"    at {duration_text(s.seconds):>8}: {ms(round(s.before, 2))} -> {ms(round(s.after, 2))} ms ({s.size:+.2f} ms)")
    else:
        out.append(f"  baseline steps: none found; level {ms(round(b.levels[0][2], 2))} ms throughout (block-to-block noise {b.noise:.2f} ms)")
    s = d.stability
    if s.tested:
        end = "the longest tested" if s.best_tau == s.taus[-1] else "beyond that it gets worse"
        out.append(f"  stability: steadiest when averaged over {duration_text(s.best_tau)} (Allan deviation {s.best_deviation:.3f} ms; {end}). "
                   f"Slope {s.short_slope:+.2f} at short times, {s.long_slope:+.2f} at long (-0.5 is plain noise, 0 or above is wander)"
                   + (f"; {s.filled} lost ping(s) filled in" if s.filled else ""))
    else:
        out.append("  stability: not tested (too few replies)")
    m = d.modes
    if m.modes:
        levels = ", ".join(f"{ms(round(mode.centre, 2))} ms ({100 * mode.share:.0f}%)" for mode in m.modes)
        out.append(f"  delay levels: {len(m.modes)}: {levels}")
    return "\n".join(out)


def figure_host(d: Deep):
    """Five panels for one host.  Returns None when there is nothing to draw."""
    import matplotlib.pyplot as plt

    if not len(d.baseline.floor):
        return None
    fig, axes = plt.subplots(3, 2, figsize=(11, 9.6), dpi=130)
    fig.patch.set_facecolor(SURFACE)
    for ax in axes.flat:
        ax.set_facecolor(SURFACE)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=8.5, length=0)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)

    def title(ax, heading: str, finding: str) -> None:
        ax.set_title(heading, loc="left", color=INK, fontsize=10.5, pad=20)
        ax.text(0, 1.03, finding, transform=ax.transAxes, color=INK_2, fontsize=8.8, va="bottom")

    def rhythm_panel(ax, r: Rhythm, heading: str) -> None:
        if not r.tested:
            title(ax, heading, f"not tested: {r.reason}")
            ax.set_xticks([]), ax.set_yticks([])
            return
        ax.plot(r.periods, 100 * r.powers, color=SERIES, linewidth=1)
        ax.plot(r.periods, 100 * r.threshold, color=INK_2, linewidth=1, linestyle=(0, (4, 3)), label="1% of shuffles reach this")
        ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, loc="upper right")
        ax.set_xscale("log")
        ax.set_xlabel("Repeat time (s)", color=INK_2, fontsize=9)
        ax.set_ylabel("% of variation", color=INK_2, fontsize=9)
        ax.set_ylim(0, max(100 * r.powers.max(), 100 * float(r.threshold.max())) * 1.35)
        title(ax, heading, (f"found: repeats every {r.period:.1f} s" if r.found else f"none found (strongest {r.strongest:.1f} s)")
              + f"; chance in shuffled data {chance_text(r.chance, True)}")

    rhythm_panel(axes[0, 0], d.rhythm_delay, "1. Rhythm in the delay")
    rhythm_panel(axes[0, 1], d.rhythm_events, "1b. Rhythm in lost and slow pings")

    ax, l = axes[1, 0], d.loss
    if l.tested:
        lengths = sorted(l.lengths)
        bursts = sum(l.lengths.values())
        ax.bar([x - 0.2 for x in lengths], [l.lengths[x] for x in lengths], width=0.38, color=SERIES, linewidth=0, label="seen")
        # If losses were independent at this rate, burst lengths would follow a geometric law.
        ax.bar([x + 0.2 for x in lengths], [bursts * (1 - l.rate) * l.rate ** (x - 1) for x in lengths], width=0.38,
               color=AXIS, linewidth=0, label="if independent")
        ax.set_xticks(lengths)
        ax.legend(frameon=False, fontsize=8.5, labelcolor=INK_2)
        ax.set_xlabel("Lost pings in a row", color=INK_2, fontsize=9)
        ax.set_ylabel("Bursts", color=INK_2, fontsize=9)
        title(ax, "2. Loss bursts", ("clumped" if l.bursty else "no clumping found") + f"; after a loss, next lost {100 * l.after_lost:.0f}% "
              f"(overall {100 * l.rate:.1f}%); chance {chance_text(l.chance)}")
    else:
        title(ax, "2. Loss bursts", f"not tested: {l.lost} lost, too few")
        ax.set_xticks([]), ax.set_yticks([])

    ax, b = axes[1, 1], d.baseline
    ax.plot(b.times, b.floor, color=SERIES, linewidth=1)
    for start, end, level in b.levels:
        ax.plot([start, end], [level, level], color=INK, linewidth=1.6)
    for s in b.steps:
        ax.axvline(s.seconds, color=CRITICAL, linewidth=1, linestyle=(0, (4, 3)))
    ax.set_xlabel("Seconds", color=INK_2, fontsize=9)
    ax.set_ylabel("Fastest ping (ms)", color=INK_2, fontsize=9)
    title(ax, "3. Baseline steps", (f"{len(b.steps)} found: " + ", ".join(f"{s.size:+.1f} ms at {s.seconds:.0f} s" for s in b.steps[:4])
                                    + (" ..." if len(b.steps) > 4 else "")) if b.steps else "none found")

    ax, s = axes[2, 0], d.stability
    if s.tested:
        ax.plot(s.taus, s.deviation, color=SERIES, linewidth=1.4, marker="o", markersize=3.5)
        ax.plot([s.best_tau], [s.best_deviation], marker="o", markersize=8, markerfacecolor="none", markeredgecolor=INK)
        ax.set_xscale("log"), ax.set_yscale("log")
        from matplotlib.ticker import FormatStrFormatter, NullFormatter
        ax.yaxis.set_major_formatter(FormatStrFormatter("%g"))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.set_xlabel("Averaging time (s)", color=INK_2, fontsize=9)
        ax.set_ylabel("Allan deviation (ms)", color=INK_2, fontsize=9)
        title(ax, "4. Stability", f"steadiest averaged over {duration_text(s.best_tau)}: {s.best_deviation:.3f} ms; "
              f"slope {s.short_slope:+.2f} short, {s.long_slope:+.2f} long")
    else:
        title(ax, "4. Stability", "not tested: too few replies")
        ax.set_xticks([]), ax.set_yticks([])

    ax, m = axes[2, 1], d.modes
    if len(m.grid):
        ax.fill_between(m.grid, m.density, color=SERIES, alpha=0.25, linewidth=0)
        ax.plot(m.grid, m.density, color=SERIES, linewidth=1.2)
        for mode in m.modes:
            ax.axvline(mode.centre, color=INK_2, linewidth=1, linestyle=(0, (4, 3)))
        ax.set_yticks([])
        ax.set_xlabel("Ping time (ms)", color=INK_2, fontsize=9)
        title(ax, "5. Delay levels", f"{len(m.modes)}: " + ", ".join(f"{ms(round(mode.centre, 1))} ms ({100 * mode.share:.0f}%)" for mode in m.modes[:5]))

    fig.suptitle(d.host, x=0.06, y=0.985, ha="left", color=INK, fontsize=13)
    fig.text(0.06, 0.955, f"{d.pings} pings over {duration_text(d.span)}, one every {d.interval:.2f} s", color=INK_2, fontsize=10)
    fig.subplots_adjust(left=0.075, right=0.975, top=0.89, bottom=0.06, hspace=0.62, wspace=0.22)
    return fig


def plot_host(d: Deep, path: Path) -> bool:
    import matplotlib.pyplot as plt

    fig = figure_host(d)
    if fig is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deeper analysis of a ping record: rhythm, loss bursts, baseline steps, stability, delay levels.")
    parser.add_argument("export", help="PingTracer CSV export, or a CSV saved by irtt_stats / twampy_stats / crusader_stats")
    parser.add_argument("--out", help="folder for the pictures (default: beside the CSV)")
    parser.add_argument("--min-period", type=float, help="shortest repeat time to look for, seconds (default: 1 s or 2.5 pings)")
    parser.add_argument("--max-period", type=float, help="longest repeat time to look for, seconds (default: 300 s or an eighth of the record)")
    parser.add_argument("--shuffles", type=int, default=200, help="shuffled copies used to judge a rhythm (default 200)")
    parser.add_argument("--use-measured", action="store_true", help="use the app's MeasuredMs instead of the operating system's RoundTripMs")
    parser.add_argument("--no-plots", action="store_true", help="print the report only")
    args = parser.parse_args(argv)

    if not args.no_plots:
        try:
            import matplotlib
            matplotlib.use("Agg")
        except ImportError:
            print("Pictures need matplotlib; continuing without them.", file=sys.stderr)
            args.no_plots = True
    from ping_histogram import safe_name

    out_dir = Path(args.out) if args.out else Path(args.export).resolve().parent
    for host, pings in read_export(args.export).items():
        if not pings:
            continue
        deep = analyse_host(host, pings, choose_times(pings, args.use_measured), args.max_period, args.min_period, args.shuffles)
        print(report(deep))
        if not args.no_plots:
            picture = out_dir / f"{Path(args.export).stem} - {safe_name(host)} - deep.png"
            if plot_host(deep, picture):
                print(f"  picture: {picture}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
