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

Still to check:

- [ ] twampy on Python 3.15 (its page says tested to 3.14).
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
