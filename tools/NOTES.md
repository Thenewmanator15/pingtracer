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

## Tools to check out

All **from memory**. None has been run here.

- [ ] **twampy** — a Python implementation of TWAMP, the standard two-way method.
      Sender on one machine, responder on the other; reports outbound, inbound and
      round-trip delay, jitter and loss. Check: does it run on Python 3.15, and what
      exactly does it print?
- [ ] **irtt** — a small tool built for this; reports send and receive delay and loss
      separately. Said to be packaged for OpenWrt, so the router could be one end.
      Check: is it available for OpenWrt 25.12, and is there a Windows build?
- [ ] **TWAMP / OWAMP in perfSONAR** — what research networks use. Probably more than
      is needed.
- [ ] **Scapy** — builds the ICMP timestamp request in Python. Needs administrator
      rights and Npcap; only your own devices answered that kind of ping.
- [ ] **ntplib** — measures how far apart two clocks are. A helper for the others,
      to put a figure on the clock error.
- [ ] **netem** — not a measuring tool: a link emulator in Linux, said to be available
      for OpenWrt. Adds a chosen delay, loss and jitter to a normal connection, giving
      a satellite-like link on the desk to test everything against known conditions.
      Check: is it packaged for OpenWrt 25.12?

## An idea not yet built: our own two-way script

A sender and a reflector trading UDP packets with the four timestamps, writing the
same CSV shape as PingTracer's export with forward and backward columns, so the
report, histogram and spike scripts here work per direction.

It could be tested before being trusted: run both ends on one PC, where there is only
one clock, have the reflector hold packets for a known time each way, and check the
script reports those figures.

For a real measurement the reflector must run on a machine at the far end. The Home
Assistant box could be one.

## Related things already found

- ICMP can be rate-limited or given low priority by routers, so a ping shows how the
  network treats *pings*. Tests: does the loss carry on to later hops, does halving
  the ping rate remove it, does a normal connection (Tools > Network Tests in the
  modified PingTracer) agree? From memory; the PingTracer README makes the same point
  about rate limiting.
- Some links compress data. A payload of zeros shrinks to almost nothing, so a large
  ping of zeros may not test a large packet. Random payload avoids that. A hypothesis
  for satellite links, not measured.
