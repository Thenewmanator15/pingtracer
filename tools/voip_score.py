#!/usr/bin/env python3
"""Sends a stream shaped like a phone call and scores how the call would sound.

A phone call sends one small packet every 20 ms in each direction.  This runs
irtt with packets of that size and spacing, then works out, for each direction,
the call-quality score phone engineers use: the "R" figure of the ITU E-model
(0 to 100) and the "MOS" it corresponds to (1 to 4.5).

On the far machine:

    irtt server

On this machine:

    py -3.15 voip_score.py 192.168.1.233 --seconds 60

To score a result irtt already saved:

    py -3.15 voip_score.py --from-json run.json

How the score is worked out (the simplified E-model):

    R   = 93.2 - (points lost to delay) - (points lost to missing packets)
    delay points   = 0.024 d + 0.11 (d - 177.3) for the part of d above 177.3 ms,
                     d being the mouth-to-ear delay
    missing points = Ie + (95 - Ie) * P / (P / BurstR + Bpl),
                     P being the percentage of packets missing
    MOS = 1 + 0.035 R + 0.000007 R (R - 60) (100 - R)

What this script has to assume, because a ping-like test cannot see it:

* The receiving phone holds packets in a buffer to even out their arrival.  A
  packet arriving later than the buffer allows is thrown away, the same as a
  lost one.  A longer buffer saves more packets but adds delay.  Unless
  --buffer is given, the buffer that gives the best score is used, as a phone
  that adapts would aim for; the picture shows the score for every buffer.
* Mouth-to-ear delay = the link's delay one way + the buffer + 20 ms to fill
  a packet.  The link's delay one way is taken as half the fastest round
  trip, because the two machines' clocks usually disagree; --trust-clocks
  uses each direction's own measured delay instead.
* Clumped loss sounds worse than scattered loss.  BurstR is how much longer
  the runs of missing packets are than chance would give (1 = scattered).
* Echo is taken as fully cancelled, and the codec figures (Ie, Bpl) are for
  G.711, the plain telephone codec, unless given.

Where the figures come from: the form of the score, 93.2, the MOS formula and
Bpl = 4.3 for G.711 were checked against ITU pages (see NOTES.md).  The delay
formula is the published short form by Cole and Rosenbluth (2001), not the
ITU's full one.  Bpl = 25.1 for G.711 with loss concealment is from memory.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

import irtt_stats
from twampy_stats import ANSWERED, LOST_BACK, LOST_OUT, LOST_UNKNOWN, Probe, write_csv

PACKET_MS = 20.0          # one packet of speech
PACKET_BYTES = 172        # 160 bytes of G.711 speech + 12 bytes of RTP header
R_BEST = 93.2             # the E-model's score with nothing wrong
CODECS = {                # name: (Ie, Bpl, description)
    "g711": (0.0, 4.3, "G.711, no loss concealment"),
    "g711plc": (0.0, 25.1, "G.711 with loss concealment"),
}
BANDS = ((90, "very satisfied"), (80, "satisfied"), (70, "some users dissatisfied"), (60, "many users dissatisfied"),
         (50, "nearly all users dissatisfied"), (-math.inf, "not recommended"))
BUFFERS = np.arange(0.0, 401.0, 5.0)


def delay_points(mouth_to_ear_ms: float) -> float:
    """Points lost to delay (Cole and Rosenbluth's short form of the E-model's delay term)."""
    extra = max(0.0, mouth_to_ear_ms - 177.3)
    return 0.024 * mouth_to_ear_ms + 0.11 * extra


def missing_points(missing_percent: float, burst_r: float, ie: float, bpl: float) -> float:
    """Points lost to missing packets (the E-model's effective equipment impairment)."""
    if missing_percent <= 0:
        return ie
    return ie + (95.0 - ie) * missing_percent / (missing_percent / burst_r + bpl)


def mos_from_r(r: float) -> float:
    if r <= 0:
        return 1.0
    if r >= 100:
        return 4.5
    return 1.0 + 0.035 * r + 7e-6 * r * (r - 60.0) * (100.0 - r)


def band(r: float) -> str:
    return next(words for floor, words in BANDS if r >= floor)


def burst_ratio(missing: np.ndarray) -> float:
    """How much longer the runs of missing packets are than chance would give.  1 = scattered; never reported below 1."""
    missing = np.asarray(missing, dtype=bool)
    before, after = missing[:-1], missing[1:]
    if not before.any() or before.all():
        return 1.0
    p = float(after[~before].mean())          # chance a packet goes missing after one that arrived
    q = float((~after[before]).mean())        # chance a packet arrives after one that went missing
    return max(1.0, 1.0 / (p + q)) if p + q > 0 else 1.0


@dataclass
class Leg:
    """One direction of the call."""
    name: str
    packets: int
    lost: int                 # never arrived
    late: int                 # arrived, but too late for the buffer
    buffer_ms: float
    buffer_chosen: bool       # True if the script picked the buffer
    one_way_ms: float
    mouth_to_ear_ms: float
    burst_r: float
    delay_cost: float
    missing_cost: float
    r: float
    mos: float
    jitter_ms: float          # mean change between one packet and the next
    r_by_buffer: np.ndarray   # the score for each buffer in BUFFERS

    @property
    def missing_percent(self) -> float:
        return 100.0 * (self.lost + self.late) / self.packets if self.packets else math.nan


def _score(delays: np.ndarray, one_way_ms: float, buffer_ms: float, ie: float, bpl: float) -> tuple[float, int, float, float, float]:
    lost = np.isnan(delays)
    spread = delays - np.nanmin(delays)
    late = ~lost & (spread > buffer_ms)
    missing = lost | late
    percent = 100.0 * missing.mean()
    burst = burst_ratio(missing)
    d_cost = delay_points(one_way_ms + buffer_ms + PACKET_MS)
    m_cost = missing_points(percent, burst, ie, bpl)
    return R_BEST - d_cost - m_cost, int(late.sum()), burst, d_cost, m_cost


def score_leg(name: str, delays, one_way_ms: float, buffer_ms: float | None = None, ie: float = 0.0, bpl: float = 4.3) -> Leg:
    """Scores one direction.  delays holds each packet's delay in ms, NaN where it never arrived.

    Only the differences between the delays are used (a clock offset shifts them all alike);
    one_way_ms is the link's delay in this direction for its fastest packet.
    """
    delays = np.asarray(delays, dtype=float)
    if delays.size == 0 or np.isnan(delays).all():
        return Leg(name, int(delays.size), int(delays.size), 0, 0.0, buffer_ms is None, one_way_ms, math.nan, 1.0, math.nan, math.nan,
                   0.0, 1.0, math.nan, np.zeros(len(BUFFERS)))
    curve = np.array([_score(delays, one_way_ms, b, ie, bpl)[0] for b in BUFFERS])
    chosen = buffer_ms is None
    if chosen:
        buffer_ms = float(BUFFERS[int(np.argmax(curve))])      # the shortest buffer among equal bests
    r, late, burst, d_cost, m_cost = _score(delays, one_way_ms, buffer_ms, ie, bpl)
    pairs = np.abs(np.diff(delays))
    pairs = pairs[~np.isnan(pairs)]
    return Leg(name, int(delays.size), int(np.isnan(delays).sum()), late, buffer_ms, chosen, one_way_ms, one_way_ms + buffer_ms + PACKET_MS,
               burst, d_cost, m_cost, r, mos_from_r(r), float(pairs.mean()) if pairs.size else math.nan, curve)


def score_call(probes: list[Probe], buffer_ms: float | None = None, codec: str = "g711", trust_clocks: bool = False,
               ie: float | None = None, bpl: float | None = None) -> tuple[Leg, Leg]:
    """Scores both directions of a run: (what the far end hears, what this end hears)."""
    codec_ie, codec_bpl, _ = CODECS[codec]
    ie = codec_ie if ie is None else ie
    bpl = codec_bpl if bpl is None else bpl
    # A probe counts in a direction if it travelled in that direction: one lost on the way out never set off back.
    # One lost on the way back did arrive at the far end, but when is not known; it is counted there as on time.
    fastest_out = min((p.outbound_ms for p in probes if p.fate == ANSWERED), default=math.nan)
    out = np.array([p.outbound_ms if p.fate == ANSWERED else fastest_out if p.fate == LOST_BACK else math.nan
                    for p in probes if p.fate in (ANSWERED, LOST_OUT, LOST_BACK)], dtype=float)
    back = np.array([p.inbound_ms if p.fate == ANSWERED else math.nan for p in probes if p.fate in (ANSWERED, LOST_BACK)], dtype=float)
    trips = [p.round_trip_ms for p in probes if p.fate == ANSWERED]
    half = min(trips) / 2 if trips else math.nan
    legs = []
    for name, delays in (("to the far end", out), ("from the far end", back)):
        one_way = float(np.nanmin(delays)) if trust_clocks and not np.isnan(delays).all() else half
        legs.append(score_leg(name, delays, one_way, buffer_ms, ie, bpl))
    return legs[0], legs[1]


def report(legs: tuple[Leg, Leg], probes: list[Probe], far_end: str, codec: str, trust_clocks: bool) -> str:
    unknown = sum(1 for p in probes if p.fate == LOST_UNKNOWN)
    out = [f"Call-shaped test to {far_end}: {len(probes)} packets each way, one every {PACKET_MS:g} ms ({CODECS[codec][2]})", ""]
    for leg in legs:
        out.append(f"{leg.name}  ({'what they hear' if leg is legs[0] else 'what you hear'})")
        if math.isnan(leg.mouth_to_ear_ms):
            out.append("  nothing arrived")
            continue
        out.append(f"  score            R {leg.r:.1f}   MOS {leg.mos:.2f}   {band(leg.r)}")
        out.append(f"  mouth to ear     {leg.mouth_to_ear_ms:.0f} ms = {leg.one_way_ms:.1f} link + {leg.buffer_ms:.0f} buffer + {PACKET_MS:.0f} packet"
                   f"{'   (buffer chosen for the best score)' if leg.buffer_chosen else ''}")
        out.append(f"  missing          {leg.missing_percent:.2f}% = {leg.lost} lost + {leg.late} too late, of {leg.packets}"
                   f"   clumping (BurstR) {leg.burst_r:.2f}")
        out.append(f"  jitter           {leg.jitter_ms:.2f} ms")
        out.append(f"  points lost      {leg.delay_cost:.1f} to delay, {leg.missing_cost:.1f} to missing packets (of {R_BEST})")
        if codec == "g711" and leg.missing_percent > 0:
            concealed = R_BEST - leg.delay_cost - missing_points(leg.missing_percent, leg.burst_r, *CODECS["g711plc"][:2])
            out.append(f"  if the phone hides gaps (loss concealment; figure from memory, not checked): R {concealed:.1f}, MOS {mos_from_r(concealed):.2f}")
        out.append("")
    worse = min(legs, key=lambda leg: leg.r)
    out.append(f"The call is as good as its worse direction: R {worse.r:.1f}, MOS {worse.mos:.2f}, {band(worse.r)}.")
    if unknown:
        out.append(f"{unknown} packet(s) were lost in a direction irtt could not tell; they are counted in neither.")
    if not trust_clocks:
        out.append("The link's delay one way is taken as half the fastest round trip.")
    return "\n".join(out)


# ------------------------------------------------------------------------ picture

SURFACE, INK, INK_2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SERIES, ACCENT = "#e1e0d9", "#c3c2b7", "#2a78d6", "#eb6834"


def plot_call(legs: tuple[Leg, Leg], probes: list[Probe], title: str, path: Path) -> bool:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colours = (SERIES, ACCENT)
    fig, axes = plt.subplots(2, 2, figsize=(12, 7.5), facecolor=SURFACE)
    fig.subplots_adjust(left=0.12, right=0.97, top=0.84, bottom=0.09, hspace=0.48, wspace=0.34)
    for ax in axes.flat:
        ax.set_facecolor(SURFACE)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(AXIS)
        ax.tick_params(colors=INK_2, labelsize=9)
        ax.grid(True, color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
    worse = min(legs, key=lambda leg: leg.r)
    fig.suptitle(title, x=0.12, ha="left", fontsize=13, color=INK, fontweight="bold")
    fig.text(0.12, 0.915, f"Worse direction: R {worse.r:.1f}, MOS {worse.mos:.2f}, {band(worse.r)}", fontsize=10.5, color=INK_2)

    # 1. how late each packet was, with the buffer
    ax = axes[0, 0]
    answered = [p for p in probes if p.fate == ANSWERED and p.sent is not None]
    if answered:
        start = answered[0].sent
        seconds = np.array([(p.sent - start).total_seconds() for p in answered])
        for leg, values, colour in zip(legs, ([p.outbound_ms for p in answered], [p.inbound_ms for p in answered]), colours):
            values = np.array(values) - min(values)
            ax.plot(seconds, values, ".", ms=2.5, color=colour, label=f"{leg.name} (buffer {leg.buffer_ms:.0f} ms)")
            ax.axhline(leg.buffer_ms, color=colour, linewidth=1, linestyle="--")
    ax.set_title("How much later than the fastest packet each one arrived", loc="left", fontsize=10.5, color=INK)
    ax.set_xlabel("seconds into the test", color=INK_2, fontsize=9)
    ax.set_ylabel("ms", color=INK_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8.5, labelcolor=INK_2, loc="upper right")

    # 2. the score for every buffer
    ax = axes[0, 1]
    for floor, words in BANDS[:-1]:
        ax.axhline(floor, color=AXIS, linewidth=0.8)
        ax.text(BUFFERS[-1], floor + 0.6, words, ha="right", va="bottom", fontsize=7.5, color=MUTED)
    for leg, colour in zip(legs, colours):
        ax.plot(BUFFERS, leg.r_by_buffer, color=colour, linewidth=2, label=leg.name)
        ax.plot([leg.buffer_ms], [leg.r], "o", ms=8, color=colour, markeredgecolor=SURFACE, markeredgewidth=2)
    ax.set_ylim(0, 100)
    ax.set_title("Score (R) for each buffer length; dot = the one used", loc="left", fontsize=10.5, color=INK, pad=22)
    ax.set_xlabel("buffer in the receiving phone, ms", color=INK_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8.5, labelcolor=INK_2, loc="lower right", bbox_to_anchor=(1.0, 1.0), ncols=2)

    # 3. missing packets
    ax = axes[1, 0]
    names = [leg.name for leg in legs]
    lost = [100 * leg.lost / leg.packets if leg.packets else 0 for leg in legs]
    late = [100 * leg.late / leg.packets if leg.packets else 0 for leg in legs]
    ax.barh(names, lost, height=0.5, color=colours, edgecolor=SURFACE, linewidth=2)
    ax.barh(names, late, left=lost, height=0.5, color=colours, alpha=0.45, edgecolor=SURFACE, linewidth=2)
    for row, leg in enumerate(legs):
        ax.text(lost[row] + late[row], row, f"  {leg.missing_percent:.2f}%  ({leg.lost} lost, {leg.late} too late)", va="center", fontsize=9, color=INK_2)
    ax.set_xlim(0, max(1.0, max(a + b for a, b in zip(lost, late)) * 1.9))
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_title("Packets missing from the call (solid: lost; pale: too late)", loc="left", fontsize=10.5, color=INK)
    ax.set_xlabel("% of packets", color=INK_2, fontsize=9)

    # 4. where the points went
    ax = axes[1, 1]
    delay = [leg.delay_cost for leg in legs]
    missing = [leg.missing_cost for leg in legs]
    ax.barh(names, delay, height=0.5, color=MUTED, edgecolor=SURFACE, linewidth=2, label="delay")
    ax.barh(names, missing, left=delay, height=0.5, color="#d03b3b", edgecolor=SURFACE, linewidth=2, label="missing packets")
    for row, leg in enumerate(legs):
        ax.text(delay[row] + missing[row], row, f"  {delay[row]:.1f} + {missing[row]:.1f}  ->  R {leg.r:.1f}", va="center", fontsize=9, color=INK_2)
    ax.set_xlim(0, max(10.0, max(a + b for a, b in zip(delay, missing)) * 1.6))
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_title(f"Points lost from the best score of {R_BEST}", loc="left", fontsize=10.5, color=INK, pad=22)
    ax.set_xlabel("points", color=INK_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8.5, labelcolor=INK_2, loc="lower right", bbox_to_anchor=(1.0, 1.0), ncols=2)

    fig.savefig(path, dpi=130, facecolor=SURFACE)
    plt.close(fig)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a stream shaped like a phone call with irtt and score the call.")
    parser.add_argument("far_end", nargs="?", help="the machine running 'irtt server', as IP or IP:port")
    parser.add_argument("--seconds", type=float, default=60, help="how long to test (default 60)")
    parser.add_argument("--irtt", type=Path, default=irtt_stats.DEFAULT_IRTT, help="path to the irtt program")
    parser.add_argument("--from-json", help="score this saved irtt result instead of running irtt")
    parser.add_argument("--buffer", type=float, help="buffer in the receiving phone, ms (default: the one giving the best score)")
    parser.add_argument("--codec", choices=sorted(CODECS), default="g711", help="default g711")
    parser.add_argument("--ie", type=float, help="the codec's Ie, to override --codec")
    parser.add_argument("--bpl", type=float, help="the codec's Bpl, to override --codec")
    parser.add_argument("--trust-clocks", action="store_true", help="use each direction's own delay; only if both clocks are matched")
    parser.add_argument("--csv", help="where to save the packets (default: voip <time>.csv in the current folder)")
    parser.add_argument("--no-plot", action="store_true", help="do not draw the picture")
    args = parser.parse_args(argv)

    if args.from_json:
        source = Path(args.from_json)
        result = json.loads(source.read_text(encoding="utf-8"))
        csv_path = Path(args.csv) if args.csv else source.with_suffix(".csv")
    elif args.far_end:
        csv_path = Path(args.csv) if args.csv else Path(f"voip {datetime.now():%Y-%m-%d %H-%M-%S}.csv")
        result = irtt_stats.run_irtt(args.far_end, args.seconds, PACKET_MS, args.irtt, csv_path.with_suffix(".json"), PACKET_BYTES)
    else:
        parser.error("give the far end as IP or IP:port, or --from-json FILE")

    probes, far_end = irtt_stats.read_result(result)
    if not probes:
        print("irtt's result holds no packets.", file=sys.stderr)
        return 1
    spacing = result.get("config", {}).get("params", {}).get("interval")
    if spacing and abs(spacing / 1e6 - PACKET_MS) > 0.5:
        print(f"Note: this result was sent with a packet every {spacing / 1e6:g} ms, not {PACKET_MS:g}. Runs of missing packets, and so the "
              "score, are judged as if each packet were one packet of speech.\n")
    legs = score_call(probes, args.buffer, args.codec, args.trust_clocks, args.ie, args.bpl)
    print(report(legs, probes, far_end, args.codec, args.trust_clocks))
    write_csv(probes, far_end, csv_path)
    print(f"\nsaved {csv_path}")
    if not args.no_plot:
        picture = csv_path.with_name(f"{csv_path.stem} - call score.png")
        try:
            plot_call(legs, probes, f"Call-shaped test to {far_end}", picture)
            print(f"picture: {picture}")
        except ImportError:
            print("The picture needs matplotlib.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
