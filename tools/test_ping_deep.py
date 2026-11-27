#!/usr/bin/env python3
"""Checks ping_deep.py on made-up records where the right answer is known, and against
published packages (Astropy, AllanTools, SciPy) where they are installed.

    py -3.15 test_ping_deep.py
"""

from __future__ import annotations

import math
import sys

import numpy as np

import ping_deep as deep

failures = 0


def check(name: str, passed: bool, detail: str) -> None:
    global failures
    failures += not passed
    print(f"  {'ok  ' if passed else 'FAIL'} {name}: {detail}")


def bursty_noise(rng, n: int, base: float = 30.0) -> np.ndarray:
    """Delay with no rhythm, but with slow stretches lasting a few pings, as a busy link gives."""
    y = base + rng.gamma(2.0, 0.4, n)
    starts = rng.choice(n - 10, n // 150, replace=False)
    for s in starts:
        y[s:s + rng.integers(2, 9)] += rng.uniform(5, 40)
    return y


def test_rhythm() -> None:
    print("1. Rhythm")
    rng = np.random.default_rng(11)
    n, interval = 6000, 0.2                                  # 20 minutes, 5 pings a second
    t = np.arange(n) * interval + rng.uniform(0, 0.01, n)
    y = bursty_noise(rng, n)
    y[(t % 15.0) < 0.6] += 6.0                               # a 6 ms bump for 0.6 s, every 15 s
    r = deep.find_rhythm("delay", t, y, interval)
    # A short bump repeating every 15 s is strongest at 15/2, 15/3, ... s; the repeat time reported must still be 15 s.
    check("planted 15 s rhythm is found", r.found and abs(r.period - 15.0) < 0.3,
          f"repeats every {r.period:.2f} s (set 15), strongest peak at {r.strongest:.2f} s, chance {r.chance:.3f}")
    smooth = bursty_noise(rng, n) + 3.0 * np.sin(2 * np.pi * t / 42.0)        # a smooth 42 s swing
    r = deep.find_rhythm("delay", t, smooth, interval)
    check("planted 42 s swing is found", r.found and abs(r.period - 42.0) < 1.5, f"repeats every {r.period:.2f} s (set 42), chance {r.chance:.3f}")

    false_alarms = 0
    runs = 20
    for seed in range(runs):
        rng = np.random.default_rng(100 + seed)
        t = np.arange(3000) * interval
        r = deep.find_rhythm("delay", t, bursty_noise(rng, 3000), interval, seed=seed)
        false_alarms += r.found
    check("no rhythm claimed in bursty noise", false_alarms <= 1, f"{false_alarms} false alarm(s) in {runs} records (test level 1 in 100)")

    try:
        from astropy.timeseries import LombScargle
    except ImportError:
        print("  skip Astropy comparison (not installed)")
        return
    rng = np.random.default_rng(5)
    t = np.sort(rng.uniform(0, 300, 800))
    y = 2 * np.sin(2 * np.pi * t / 12.5) + rng.normal(0, 1, 800)
    frequencies = np.linspace(0.01, 1.0, 500)
    mine = deep.lomb_scargle(t, y, frequencies)
    theirs = LombScargle(t, y, fit_mean=False, center_data=True).power(frequencies, normalization="standard", method="slow")
    check("periodogram equals Astropy's", float(np.abs(mine - theirs).max()) < 1e-9,
          f"largest difference {np.abs(mine - theirs).max():.1e} over 500 frequencies; peak at {1 / frequencies[mine.argmax()]:.2f} s (set 12.5)")


def gilbert(rng, n: int, p: float, r: float) -> np.ndarray:
    lost = np.zeros(n, dtype=bool)
    state = False
    for i in range(n):
        state = (rng.random() >= r) if state else (rng.random() < p)
        lost[i] = state
    return lost


def test_loss_bursts() -> None:
    print("2. Loss bursts")
    rng = np.random.default_rng(3)
    lost = gilbert(rng, 60000, p=0.01, r=0.25)               # set: bursts average 1/0.25 = 4 pings
    b = deep.loss_bursts(lost)
    check("clumped loss is recognised", b.bursty, f"runs z {b.runs_z:.1f}, chance {b.chance:.1e}")
    check("chance of loss after a good ping", abs(b.after_ok - 0.01) < 0.002, f"measured {b.after_ok:.4f}, set 0.0100")
    check("chance of loss after a lost ping", abs(b.after_lost - 0.75) < 0.03, f"measured {b.after_lost:.3f}, set 0.750")
    check("mean burst length", abs(b.mean_burst - 4.0) < 0.3, f"measured {b.mean_burst:.2f}, set 4.00")

    false_alarms = 0
    for seed in range(20):
        independent = np.random.default_rng(200 + seed).random(20000) < 0.03
        false_alarms += deep.loss_bursts(independent).bursty
    check("independent loss is not called clumped", false_alarms <= 1, f"{false_alarms} false alarm(s) in 20 records")

    try:
        from scipy.stats import norm
    except ImportError:
        print("  skip SciPy comparison (not installed)")
        return
    one = deep.loss_bursts(np.random.default_rng(9).random(5000) < 0.05)
    theirs = 2 * norm.sf(abs(one.runs_z))
    check("runs-test chance equals SciPy's normal tail", abs(one.chance - theirs) < 1e-12, f"{one.chance:.6f} against {theirs:.6f}")


def test_steps() -> None:
    print("3. Baseline steps")
    rng = np.random.default_rng(7)
    n, interval = 6000, 0.2
    t = np.arange(n) * interval
    level = np.where(t < 400, 30.0, np.where(t < 800, 36.0, 33.0))       # +6 ms at 400 s, -3 ms at 800 s
    y = level + rng.gamma(2.0, 0.5, n)
    b = deep.baseline_steps(t, y, interval)
    found = [(s.seconds, s.size) for s in b.steps]
    right = len(found) == 2 and abs(found[0][0] - 400) <= 6 and abs(found[0][1] - 6) < 0.5 and abs(found[1][0] - 800) <= 6 and abs(found[1][1] + 3) < 0.5
    check("two planted steps are found", right, ", ".join(f"{size:+.2f} ms at {at:.0f} s" for at, size in found) + " (set +6 at 400, -3 at 800)")

    false_steps = 0
    for seed in range(20):
        r = np.random.default_rng(300 + seed)
        false_steps += len(deep.baseline_steps(t, 30 + r.gamma(2.0, 0.5, n), interval).steps)
    check("no steps claimed on a level record", false_steps == 0, f"{false_steps} false step(s) in 20 records")

    whole = np.round(1.5 + rng.normal(0, 0.3, n))                         # whole-millisecond data flipping between 1 and 2
    check("rounding to whole ms is not called a step", not deep.baseline_steps(t, whole, interval).steps, "level about 1.5 ms, recorded as 1 or 2")


def test_stability() -> None:
    print("4. Stability")
    rng = np.random.default_rng(13)
    noise = rng.normal(0, 1, 20000)
    taus, dev = deep.allan_deviation(noise, rate=5.0)
    slope = np.polyfit(np.log(taus), np.log(dev), 1)[0]
    check("plain noise falls at slope -0.5", abs(slope + 0.5) < 0.05, f"measured {slope:+.3f}")
    walk = np.cumsum(rng.normal(0, 1, 20000))
    taus, dev = deep.allan_deviation(walk, rate=5.0)
    slope = np.polyfit(np.log(taus), np.log(dev), 1)[0]
    check("a wandering level rises at slope +0.5", abs(slope - 0.5) < 0.08, f"measured {slope:+.3f}")

    try:
        import allantools
    except ImportError:
        print("  skip AllanTools comparison (not installed)")
        return
    mixed = noise + 0.02 * walk
    factors = np.array([1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024])
    taus, dev = deep.allan_deviation(mixed, rate=5.0, factors=factors)
    taus2, dev2, _, _ = allantools.oadev(mixed, rate=5.0, data_type="freq", taus=factors / 5.0)
    worst = float(np.abs(dev / dev2 - 1).max())
    check("Allan deviation equals AllanTools'", len(dev) == len(dev2) and worst < 1e-9, f"largest relative difference {worst:.1e} over {len(dev)} averaging times")


def test_modes() -> None:
    print("5. Delay levels")
    rng = np.random.default_rng(17)
    y = np.concatenate([30 + rng.gamma(2.0, 0.4, 7000), 45 + rng.gamma(2.0, 0.4, 3000)])    # two levels, 70% and 30%
    m = deep.delay_modes(y).modes
    right = len(m) == 2 and abs(m[0].centre - 30.4) < 0.5 and abs(m[1].centre - 45.4) < 0.5 and abs(m[0].share - 0.7) < 0.02
    check("two planted levels are found", right, ", ".join(f"{mode.centre:.2f} ms ({100 * mode.share:.0f}%)" for mode in m) + " (set near 30.4 at 70%, 45.4 at 30%)")
    one = deep.delay_modes(30 + rng.gamma(2.0, 0.6, 8000)).modes
    check("one level is not split", len(one) == 1, f"{len(one)} level(s)")
    whole = deep.delay_modes(np.round(8 + rng.gamma(2.0, 1.2, 8000))).modes
    check("whole-ms data is not split at every whole number", len(whole) == 1, f"{len(whole)} level(s)")


def main() -> int:
    for test in (test_rhythm, test_loss_bursts, test_steps, test_stability, test_modes):
        test()
    print("\nTESTS PASSED" if not failures else f"\n{failures} CHECK(S) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
