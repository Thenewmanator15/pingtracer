# Link test tools

Tools that measure a link in each direction, score it as a phone call, compare kinds
of traffic, and Python scripts that analyse and plot what they record.

The ready-to-use folder is `dist\link-test-tools`. Copy the whole folder to the second
machine too: it holds the far end of each tool. The commands below are run from inside
it. Its `scripts` folder is a copy of `tools\` in the repository.

What has and has not been checked is in `NOTES.md`. The short version:
**everything here has only been run with both ends on one PC.** Nothing has crossed
two machines or a real link yet.

## What each test answers

| Question | Far machine | This machine | Tried |
|---|---|---|---|
| Delay, jitter and loss, each way | `far-end-irtt.cmd` | `.\test-irtt FAR-IP 60` | one PC only |
| The same, by a second tool, as a cross-check | `far-end-twampy.cmd` | `.\test-twampy FAR-IP 600` | one PC only |
| How would a phone call sound? | `far-end-irtt.cmd` | `.\test-voip FAR-IP 60` | one PC only |
| What happens when the link is full? | `far-end-crusader.cmd` | `.\test-crusader FAR-IP` | one PC only, not loaded |
| Are ping, UDP and TCP treated alike? | nothing | `.\test-trippy FAR-IP` | never run |
| Many hops over hours | nothing | PingTracer, then export CSV | home network |

Ports the far machine must let in: irtt UDP 2112, twampy UDP 20001, Crusader TCP and
UDP 35481. Windows may ask about the firewall the first time.

Results go in `results`. To analyse any of them, drag the file onto `analyse.cmd`.
It takes a PingTracer CSV, an irtt `.json`, a Crusader `.crr` or a twampy log.

## What the analysis reports

- **Report:** fastest, median, 95%, worst 1%, jitter, loss and outages for each direction.
- **Histogram and spikes:** the spread of delays, and when spikes happened.
- **Deeper analysis** (`ping_deep.py`): is there a repeating rhythm, do losses come in
  clumps, did the baseline step to a new level, how steady is the delay, how many
  levels does it sit at. Each answer has a figure for how sure it is.
- **Call score** (`voip_score.py`): R from 0 to 100 and MOS from 1 to 4.5 for each
  direction. As a guide: R 80 and above, users are satisfied; below 70, many are not;
  below 50, not recommended.

`results\made-up-satellite-like.csv` is a made-up record with a rhythm, steps and
clumped loss planted, to see what a positive result looks like.

## Things to know before trusting a number

- **Clocks.** If the two machines' clocks disagree by 5 ms, the way out reads 5 ms
  long and the way back 5 ms short. Round trip, jitter and loss are not affected.
- **The call test sends call-shaped packets, not real calls.** A 172-byte UDP packet
  every 20 ms, the size and pace of a G.711 call. A network that looks for real call
  set-up messages may not treat it as a call.
- **Nothing here is marked as voice.** Windows ignores a program's request to mark its
  packets; it takes an Administrator setting (a QoS policy).
- **Trippy needs Administrator.** `test-trippy.cmd` asks for it.
- **The call score's delay formula** is the widely quoted short form, not checked
  against the ITU's own text.

## Testing ideas

These are ideas, not results. None has been run. "Needs" says what stands in the way.

### A. First, prove the tools on two machines

| # | Idea | How | What it would show |
|---|---|---|---|
| A1 | Two machines on the home network | Far end on a second PC, `.\test-irtt FAR-IP 60` | That it works at all across machines; expect about 1 ms and no loss |
| A2 | Swap the ends | Run A1, then run it from the other machine | A delay that follows the machine and not the direction is the clocks disagreeing |
| A3 | Two tools, same link | `.\test-irtt` then `.\test-twampy`, straight after | Loss and fastest delay should agree; if not, one tool is wrong |
| A4 | Rehearse a slow link on the desk | `py -3.15 scripts\udp_delay_relay.py --listen 2113 --to FAR-IP:2112 --out-ms 270 --back-ms 270 --out-loss 0.01`, then `.\test-voip 127.0.0.1:2113` | What the reports and pictures look like at satellite-like delay, before the real link. Command tried on one PC, 28 November 2026: R 63 |

### B. Which traffic gets priority

Priority usually only shows when the link is busy (general knowledge, not measured
here). So each of these is run twice: link quiet, then link full.

| # | Idea | How | What it would show | Needs |
|---|---|---|---|---|
| B1 | Ping, UDP and TCP, quiet | `.\test-trippy FAR-IP` | The starting point: are the three already different? | You to allow Administrator; a reader for its files (not written) |
| B2 | The same, link full | Start `crusader\crusader.exe test FAR-IP` in one window, B1 in another | The kind whose delay and loss get worse least is the one being favoured | Crusader's far end; overlap the two by hand |
| B3 | Call-shaped stream, quiet and full | `.\test-voip FAR-IP 30` alone, then during a Crusader load | Whether call-sized packets ride through a full link better than the load does | as B2 |
| B4 | Does the port matter? | Far end: `irtt\irtt.exe server -b :5004`. Here: `.\test-voip FAR-IP:5004`, then the usual port | Some networks sort traffic by port number. Same score on both ports means this one does not | nothing |
| B5 | Marked as voice against unmarked | The same test with and without a voice mark on the packets | Whether the network honours the mark | You to set a QoS policy as Administrator; a far end that is not irtt's Windows server (it answers "no DSCP support") |
| B6 | Real call against call-shaped | A real-call tool (pjsua or StarTrinity SIP Tester) and `.\test-voip`, same link, link full | Your point: whether real VoIP messages are treated better than look-alike packets | A real-call tool; none is set up |
| B7 | Is ping held back? | PingTracer at 1 a second, then 5 a second, same hop | Loss that appears only at the faster rate is the router rationing pings, not the link | nothing |

### C. Character of a satellite-type link

| # | Idea | How | What it would show |
|---|---|---|---|
| C1 | Long recording | PingTracer or `.\test-irtt FAR-IP 3600`, then `analyse` | A rhythm (for example a hand-over every so many seconds), baseline steps, clumped loss |
| C2 | Same test at set times | A 10-minute `.\test-irtt` morning, evening, night, and in rain | Whether the link changes with the time of day or the weather |
| C3 | Up against down | Compare "outbound" and "inbound" in any irtt report | Which direction loses packets and which has the spikes |
| C4 | Small against large packets | PingTracer payload 32 bytes, then 1400 | Loss or delay that grows with size |
| C5 | Zeros against random data | PingTracer (your build) data pattern: Zeros, then Random, at 1400 bytes | Hypothesis, untested: a link that compresses would carry zeros faster |
| C6 | A ten-minute call | `.\test-voip FAR-IP 600` | The score over a whole call, and how clumped the missing packets are |

### D. Checks on the tools themselves

| # | Idea | How |
|---|---|---|
| D1 | Run the built-in checks | `py -3.15 scripts\test_ping_tools.py`, and the same for `test_ping_deep`, `test_irtt_stats`, `test_twampy_stats`, `test_ping_twoway`, `test_voip_score` |
| D2 | Call score against the ITU's calculator | Enter the same delay and loss in the ITU's online E-model calculator and compare R. Not done |
| D3 | Crusader against known delays | Not possible with the relay here (it carries UDP only); needs a different link emulator |

A sensible order: A1, A2, A3, then B1 to B4, then C1.

## What is in the folder

- `irtt` 0.9.2, built from source (GPL-2.0)
- `twampy` 1.3.2 source (BSD-3-Clause; runs with Python, nothing to install)
- `crusader` 0.3.2 (MIT or Apache-2.0); `crusader-gui.exe` is its window version
- `trippy` 0.13.0 (Apache-2.0)
- `scripts`: the Python scripts, a notebook, and `NOTES.md`. They need Python 3.15
  with NumPy and Matplotlib; the checks also use SciPy, Astropy and AllanTools.

| Script | Job |
|---|---|
| `analyse.py` | Picks the right script for any result file |
| `irtt_stats.py`, `twampy_stats.py`, `crusader_stats.py` | Run or read one tool, give the report and a CSV |
| `voip_score.py` | Call-shaped stream and call score |
| `ping_report.py`, `ping_histogram.py`, `ping_spikes.py`, `ping_deep.py` | Report, histogram, spikes, deeper analysis |
| `ping_twoway.py` | Our own two-way probe (the ready-made tools replaced it) |
| `udp_delay_relay.py` | A pretend link with a set delay and loss each way, for checking the tools |
| `test_*.py` | Checks, with made-up data of known contents |

Noted, not downloaded: StarTrinity SIP Tester (real calls, free with limits, not open
source) and pjsua (real calls, free, open source, would need building). See `NOTES.md`.
