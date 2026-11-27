# Notes: forward and backward latency

Things to check out, kept beside the notebook. Each line says whether it has been
**checked** (against a source or by a test here) or is **from memory** and still
needs checking.

## What Windows gives you: round trip only

Checked against Microsoft's documentation on 26 November 2026.

| Layer | What it reports | Source |
|---|---|---|
| The `ping` command | Replies "along with round-trip times" | <https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/ping> |
| The Windows function underneath (`IcmpSendEcho`, which PingTracer uses) | Address, status, round trip time in whole milliseconds, the returned data and its size, header options. No outbound time, no return time, no "received at" stamp. | <https://learn.microsoft.com/en-us/windows/win32/api/ipexport/ns-ipexport-icmp_echo_reply> |
| .NET `PingReply.RoundtripTime` (what PingTracer's code reads) | One value: milliseconds to send the request and get the reply. Reported as 0 on failure, which is also a valid success value, so the status must be checked. | <https://learn.microsoft.com/en-us/dotnet/api/system.net.networkinformation.pingreply.roundtriptime> |

So an ordinary ping cannot be split into forward and backward. Windows never has
those two numbers.

Not checked: the full list of other members on .NET's `PingReply`. From the code
they are address, status, returned data and options; none is a time.

## What a ping round trip contains

1. the trip out
2. the far end turning it round (usually a fraction of a millisecond; longer on a busy router)
3. the trip back

All timed by the sender alone, so the three cannot be separated.

Checked here against a Wireshark capture (180 pings): Windows' round trip figure was
within 0.9 ms of the wire. PingTracer's own `Sent` and `Received` stamps were within
3.4 ms of the wire.

## The exception: the timestamp option

`ping /s` asks each hop to write its arrival time into the packet (count 1 to 4).

Checked here: the router (192.168.1.1) and the Home Assistant box (192.168.1.233)
answered with their clock times. Cloudflare, Google, a host in Australia and the BBC
did not reply at all. So it works on your own network only.

The separate ICMP *timestamp request* (type 13) needs administrator rights on
Windows. Checked here: refused without them.

## How to get forward and backward: four timestamps

Needs something running at **both** ends.

1. sender notes the time and sends
2. far end notes when it arrived
3. far end notes when it sends the reply
4. sender notes when the reply arrived

Forward = 2 − 1. Backward = 4 − 3.

### The limit

Times 2 and 3 are on the other machine's clock. If that clock is 5 ms ahead, forward
reads 5 ms long and backward 5 ms short. The sum is always right.

- **Valid even when the clocks disagree:** loss in each direction, jitter in each
  direction, which direction a spike was in. A constant clock error cancels out of
  anything that is a change.
- **Only as good as the clock agreement:** the actual milliseconds each way. Fine for
  a 600 ms satellite hop on machines sharing a time server; not for a 4 ms line.

This is reasoning, not something measured here yet.

## Our own two-way script: built and tested

`ping_twoway.py` does the four timestamps. Run `reflect` on the far machine and `send`
on this one. It writes the same CSV shape as PingTracer's export, with forward,
backward and round trip as three "hosts", so the report, histogram and spike scripts
work on it unchanged.

Checked here on 26 November 2026 by running both ends on this PC, where there is only
one clock, with known delays and losses injected (`test_ping_twoway.py`):

| Injected | Measured |
|---|---|
| nothing | forward 0.4 ms, backward 0.2 ms |
| 30 ms out, 80 ms back | forward 31.8 ms, backward 81.0 ms |
| 80 ms out, 30 ms back | forward 82.0 ms, backward 31.3 ms |
| every 4th probe lost on the way out | 10 of 40 lost out, 0 back |
| every 5th reply lost on the way back | 0 lost out, 8 of 40 back |
| both at once | 10 lost out, 6 back |

A made-up link with a 60 ms burst on the way back only, one second in every eight:
the spike script found 5 spikes on backward, none on forward, spaced every 8.0 s, and
a correlation between the two directions of +0.01.

Not checked: two separate machines. That needs the reflector running on a far end,
and brings in the clock difference between the two.

To run the reflector on the far machine (any Python 3.8 or newer, nothing to install):

    python ping_twoway.py reflect

It listens on UDP port 47800, which must be allowed through to that machine.

## Existing apps that do this

Checked against each project's own page on 26 November 2026, except where marked.

| App | Each direction separately? | Windows | Far end needs | Notes |
|---|---|---|---|---|
| **twampy** (Python, BSD-3-Clause) <https://github.com/nokia/twampy> | Yes: Outbound, Inbound and Roundtrip rows, each with delay, jitter and loss | Yes, listed as fully supported | `twampy responder` | Implements TWAMP, TWAMP Light and STAMP. Python 3.11 or newer, tested with 3.11 and 3.14, standard library only. Software timestamps, stated accuracy 100 µs to 2 ms. |
| **irtt** (Go, GPL-2.0) <https://github.com/heistp/irtt> | Yes: one-way delay, and loss split into upstream and downstream | Not stated outright; the code has Windows timing support | an irtt server | One-way delay needs synchronised clocks; round trip and jitter do not. Version 0.9.2. |
| **perfSONAR** <https://docs.perfsonar.net/> | Not confirmed from the page read | Not stated | its own servers | A collection of measurement software used by research networks. The page links to OWAMP configuration; TWAMP was not mentioned on it. |

TWAMP itself is an internet standard: RFC 5357, "A Two-Way Active Measurement Protocol
(TWAMP)", <https://www.rfc-editor.org/rfc/rfc5357>.

### twampy tried here, 27 November 2026

Downloaded from <https://github.com/nokia/twampy> (version 1.3.2, commit 885b986, 5 March
2026) into `dist\third-party\twampy`. Its code was read first: one file, standard
library sockets only. It ran on Python 3.15 without being installed.

Both ends were run on this PC with `udp_delay_relay.py` (in this folder) between them,
holding packets for a known time each way and losing a known share. Our own
`ping_twoway.py` was run over the same relay for comparison. 100 probes each.

| Set on the relay | twampy: out / back | ping_twoway: out / back | Loss reported |
|---|---|---|---|
| nothing | 0.40 / 0.28 ms | 1.76 / 1.47 ms | none, both |
| 30 ms out, 80 ms back | 30.8 / 80.8 ms | 32.3 / 82.2 ms | none, both |
| 80 ms out, 30 ms back | 80.7 / 30.7 ms | 82.6 / 32.6 ms | none, both |
| 30/80 ms, 21 lost on the way out | 31.3 / 81.1 ms | 32.6 / 82.4 ms | both: 21 out, 0 back |
| 30/80 ms, 18 lost on the way back | 31.2 / 81.0 ms | 32.6 / 82.3 ms | both: 0 out, 18 back |

Both tools put the delay and the loss in the right direction every time.

ping_twoway reads about 1.3 ms higher each way in this table. That is the test
arrangement, not the tool: its sender, reflector and the relay all ran inside one
Python process and took turns, while twampy's two ends were separate processes. Run
directly with no relay, ping_twoway measured 0.4 / 0.2 ms and twampy 0.11 / 0.10 ms.

Things to know about twampy, from reading its code:

- It never reports a negative delay: anything below zero is shown as zero. With two
  machines whose clocks disagree, one direction can read 0 and so hide the clock error.
  Its own README example shows an inbound of 0. ping_twoway prints the raw figure and
  an estimate of the clock difference.
- Jitter is the smoothed kind from RFC 1889, not the plain mean change used by
  PingTracer and the scripts here, so the two jitter figures are not comparable.
- The reflector stamps arrival and departure with the same reading.
- It prints a summary table. Per-probe figures only appear as log lines in verbose
  mode; there is no CSV. To use the histogram and spike scripts on a twampy run, those
  log lines would need converting.
- Probe interval is 100 ms or more, at most 9,999 probes per run.

To run it from the download without installing, from inside its `src` folder
(responder on the far machine, sender here; the addresses are far end, then near end):

    python -m twampy responder :20001
    python -m twampy sender FAR-IP:20001 :20000 -c 600 -i 100

Not checked: two separate machines.

### Full statistics from a twampy run

twampy prints one summary table. `twampy_stats.py` (in this folder) runs its sender in
verbose mode, reads the line it logs for each probe, and reports full statistics for the
way out, the way back and the round trip: baseline, median, mean, 95%, worst 1%, max,
jitter, loss and outages. It saves a CSV the histogram and spike scripts read as they are.

    py -3.15 twampy_stats.py FAR-IP:20001 --count 600 --interval 100 --plots

Checked on 27 November 2026 (`test_twampy_stats.py`):

- On a made-up log with known contents: each missing probe placed in the right direction,
  and the one lost after the last reply reported as "direction unknown".
- On a real twampy run of 150 probes over the relay (30 ms out, 80 ms back, 10% loss each
  way): losses matched the relay's own count exactly (19 out, 10 back), the medians were
  30.9 and 80.8 ms, and the means agreed with twampy's own table to 0.01 ms.
- It warns when a direction piles up at exactly 0, which is how a clock difference shows
  in twampy's figures.

Limits: figures come from twampy's log, rounded to 0.01 ms. Between two answered probes
the number lost each way is known, but not which individual probe went which way.

### irtt, built and tried

Checked on 27 November 2026. irtt's author publishes source only, no Windows program
(its install page says: install Go, then build). Built here as version 0.9.2 with Go
1.27.4 (unpacked in `G:\dev\tools\go`, not installed); the program is
`dist\third-party\irtt\irtt.exe`. Ubuntu and Debian carry irtt as a package.

`irtt_stats.py` (in this folder) runs it, or reads a result it saved, and gives the same
report, CSV and pictures as `twampy_stats.py`.

    far machine:   irtt server
    this machine:  py -3.15 irtt_stats.py FAR-IP --seconds 60 --interval 100 --plots

Both tools over the same emulated link, one after the other (30 ms out, 80 ms back, 3%
and 5% loss, same random seed, probes every 100 ms for 30 s):

| | Set on the link | irtt 0.9.2 | twampy 1.3.2 |
|---|---|---|---|
| Lost on the way out | 13 | 13 | 13 |
| Lost on the way back | 15 | 15 | 15 |
| Way out, fastest / median | 30 | 30.48 / 32.50 ms | 30.46 / 30.86 ms |
| Way back, fastest / median | 80 | 80.50 / 82.25 ms | 80.34 / 80.77 ms |
| Jitter out / back | none set | 0.68 / 0.61 ms | 0.35 / 0.29 ms |

- Losses: both exact. irtt marks every lost probe with its direction; twampy only gives
  the count for each direction.
- The fastest probes agree. irtt's medians read about 1.5 ms higher each way. Not
  explained: with no emulated link in between irtt read a median of 0.34 ms out and
  0.27 ms back, so irtt's own overhead does not cover it. Hypothesis, untested: the
  emulator (a Python program that sleeps) held packets longer during the irtt run.
- irtt saves every probe as JSON with all four time stamps from both a wall clock and a
  steady clock, and does not clamp a negative delay to zero. `irtt_stats.py` says so
  when a direction reads below zero.
- `test_irtt_stats.py`: a made-up result with known contents, then a real run (149
  probes, 10% loss each way): 19 lost out, 10 back, the emulator's count exactly.

Not tested: two separate machines, an irtt server on the router.

### Crusader, tried

Checked on 27 November 2026. Free, open source (MIT or Apache-2.0), ready-made Windows
program: <https://github.com/Zoxc/crusader>, version 0.3.2 (October 2024), unpacked in
`dist\third-party\crusader`. A different job from irtt: it loads the link with traffic
(download, upload, both) and plots speed, latency up and down, and loss while loaded.
`crusader test --idle` measures latency only.

One run, both ends on this PC, latency only, 10 s: round trip 1.0 ms (0.4 down, 0.7
up), 0% loss, picture saved as `dist\analysis-demo\crusader-idle-demo.png`. It uses one
port for both TCP and UDP, so the UDP-only emulator here cannot sit in between; its
figures have not been checked against known delays. `crusader export` turns a result
into JSON (not tried).

### Deeper analysis: ping_deep.py

Added 27 November 2026, for links that change over time (a satellite-type link is the
case in mind). Five measurements per host, each with a figure for how sure it is. It
reads a PingTracer export and the CSVs the irtt, twampy and Crusader scripts save;
`analyse.py` runs it after the ordinary report.

    py -3.15 ping_deep.py export.csv

| Measurement | Method | What a positive result looks like |
|---|---|---|
| Rhythm | Lomb-Scargle periodogram; each peak judged against its local background, and against 200 shuffled copies of the record; repeat time found by folding | "repeats every 15.0 s, chance in shuffled data less than 1 in 200" |
| Loss bursts | Two-state (Gilbert) model and a runs test | "after a lost ping the next is lost 73% of the time, overall 3.4%" |
| Baseline steps | Fastest ping in each 2 s block, split by binary segmentation | "+6.01 ms at 7 min 28 s" |
| Stability | Overlapping Allan deviation | "steadiest averaged over 13 s; wanders beyond that" |
| Delay levels | Peaks of the smoothed distribution | "3 levels: 39 ms (49%), 45 ms (37%), 54 ms (14%)" |

Checked (`test_ping_deep.py`, all pass):

- Made-up records with known contents: a 0.6 s bump every 15 s reported as 15.00 s; a
  smooth 42 s swing as 41.98 s; loss made with bursts averaging 4 pings measured at
  4.06; steps of +6 and -3 ms found at the right times as +5.99 and -2.97; two delay
  levels at 70% / 30% found as 70% / 30%.
- False alarms, 20 made-up records each with nothing planted: 0 rhythms in bursty noise,
  0 clumped verdicts on independent loss, 0 steps on a level record. Whole-millisecond
  data (Windows ping) is not read as steps or as many levels.
- Against published packages (Astropy 8.0.3, AllanTools 2024.06, SciPy 1.18.1, installed
  into Python 3.15 that day): the periodogram equals Astropy's to 1e-15, the Allan
  deviation equals AllanTools' to 1e-15, the runs-test chance equals SciPy's normal tail.

On the real 15-minute recording (`dist/analysis-demo/long-export.csv`, home network,
not a satellite link): no loss, no baseline steps, one delay level per host. One rhythm
out of eight tests passed, on the provider's first hop: 0.4% of the variation, strongest
at 1.6 s. With eight tests at 1 in 200 each, one false pass has about a 4% chance, so
this is weak. Not followed up.

Limits, as measured or known:

- A rhythm "found" means a narrow peak that shuffled data does not produce. It says
  something repeats, not what. Repeat times tested run from 1 s to an eighth of the
  record (at most 300 s) unless set otherwise.
- The delay-levels count depends on a rule (a dip to half the lower peak separates two
  levels). On the made-up satellite-like record it reported 3 levels where 6 were
  planted close together.
- Stability fills lost pings in with straight lines, and says how many.
- irtt named the direction of every lost probe in most runs; in one run a probe lost at
  the very end came back as "direction unknown". `irtt_stats.py` also prints irtt's
  count of out-of-order replies and duplicates (0 and 0 over the emulator).
- Never run on a real satellite link. `dist/analysis-demo/made-up-satellite-like.csv`
  is a made-up record with a rhythm, steps, clumped loss and a second level planted,
  for seeing what the pictures look like when something is there.

Still to check:

- [ ] irtt for OpenWrt 25.12. The OpenWrt package page refused an automated read.
- [ ] **Scapy** and **ntplib** (from memory): Scapy builds the ICMP timestamp request
      but needs administrator rights; ntplib measures how far apart two clocks are.
- [ ] **netem** (from memory): a link emulator in Linux, said to be available for
      OpenWrt. Adds a chosen delay, loss and jitter to a normal connection, giving a
      satellite-like link on the desk.

## A fault found in PingTracer while testing, and fixed

Closing PingTracer while it is pinging crashed it on the way out, with exit codes
0xC0000005 (access violation) and 0xC0000374 (heap corruption), both inside Windows'
ntdll. It was in the author's untouched 2.1 as well, so it did not come from the
changes here.

**Cause, checked on 26 and 27 November 2026.** The process must not end while pings
are still waiting for replies. A 40-line program with nothing of PingTracer in it,
which starts 20 pings and exits, crashed 4 times in 5; the same program waiting for
the replies first was clean 5 times in 5. Disposing the Ping objects, or suppressing
their finalizers, before exiting did not help. A trace of PingTracer itself showed it
exiting with 2 to 5 pings in flight.

Why Windows or .NET crashes in that situation was not investigated; only the
condition was pinned down.

**Fix, in the modified build.** On close the app stops pinging, hides its windows at
once, and closes for real when the last ping has completed. It gives up after the ping
timeout plus two seconds.

| Situation | Author's 2.1 | With the fix |
|---|---|---|
| Closed by the X while pinging four hosts | crashed 5 of 6 | clean 10 of 10 |
| Task Manager style close | crashed | clean 6 of 6 |
| A host that never answers | crashed 1 of 4 | clean 4 of 4, exits when the timeout expires |
| Closed during the startup route trace | clean 4 of 4 | clean 4 of 4 |
| Stop, then close | clean 10 of 10 | clean 4 of 4 |
| Closed without ever pinging | clean 3 of 3 | clean 4 of 4 |

A lesson from getting there: the first test of the fix "failed" because the script's
way of closing the window looks to the app like a Task Manager close, which the first
version of the fix skipped. Closing must be tested the way a person does it (the
system menu's Close), not only with a bare close message.

Built here against .NET Framework 4.7.2; the official download was not tested.
## Related things already found

- ICMP can be rate-limited or given low priority by routers, so a ping shows how the
  network treats *pings*. Tests: does the loss carry on to later hops, does halving
  the ping rate remove it, does a normal connection (Tools > Network Tests in the
  modified PingTracer) agree? From memory; the PingTracer README makes the same point
  about rate limiting.
- Some links compress data. A payload of zeros shrinks to almost nothing, so a large
  ping of zeros may not test a large packet. Random payload avoids that. A hypothesis
  for satellite links, not measured.
