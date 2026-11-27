#!/usr/bin/env python3
"""A tiny link emulator for testing two-way latency tools on one machine.

It sits between a sender and a reflector and holds every UDP packet for a set
time, a different time in each direction, and can lose a share of them:

    sender  --->  relay (listen port)  --->  reflector
    sender  <---  relay                <---  reflector

    py -3.15 udp_delay_relay.py --listen 20002 --to 127.0.0.1:20001 --out-ms 30 --back-ms 80

Point the sender at the relay's port instead of the reflector's.  Because both
ends are on the same machine there is only one clock, so whatever the tool
reports can be compared with the delays set here.

It works for any tool that sends UDP to one address and expects the replies
from that same address, such as ping_twoway.py and twampy's sender/responder.
One sender at a time.
"""

from __future__ import annotations

import argparse
import random
import socket
import sys
import threading
import time


class Relay:
    def __init__(self, listen_port: int, target: tuple[str, int], out_ms: float = 0.0, back_ms: float = 0.0,
                 out_loss: float = 0.0, back_loss: float = 0.0, listen_host: str = "127.0.0.1", seed: int | None = None) -> None:
        self.front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # faces the sender
        self.front.bind((listen_host, listen_port))
        self.back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)    # faces the reflector
        self.back.bind((listen_host, 0))
        for sock in (self.front, self.back):
            sock.settimeout(0.2)
        self.port = self.front.getsockname()[1]
        self.target = target
        self.out = out_ms / 1000.0
        self.back_delay = back_ms / 1000.0
        self.out_loss = out_loss
        self.back_loss = back_loss
        self.random = random.Random(seed)
        self.client: tuple | None = None
        self.stopping = threading.Event()
        self.threads: list[threading.Thread] = []
        self.counts = {"out": 0, "out_lost": 0, "back": 0, "back_lost": 0}

    @staticmethod
    def _later(delay: float, send, data: bytes, address: tuple) -> None:
        def deliver() -> None:
            if delay > 0:
                time.sleep(delay)
            try:
                send(data, address)
            except OSError:
                pass
        # One thread per held packet, so a held packet never delays the one behind it.
        threading.Thread(target=deliver, daemon=True).start()

    def _pump(self, source: socket.socket, outward: bool) -> None:
        while not self.stopping.is_set():
            try:
                data, address = source.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                continue   # Windows reports a refused UDP send here; it is not fatal
            if outward:
                self.client = address
                self.counts["out"] += 1
                if self.random.random() < self.out_loss:
                    self.counts["out_lost"] += 1
                    continue
                self._later(self.out, self.back.sendto, data, self.target)
            elif self.client is not None:
                self.counts["back"] += 1
                if self.random.random() < self.back_loss:
                    self.counts["back_lost"] += 1
                    continue
                self._later(self.back_delay, self.front.sendto, data, self.client)

    def start(self) -> "Relay":
        self.threads = [threading.Thread(target=self._pump, args=(self.front, True), daemon=True),
                        threading.Thread(target=self._pump, args=(self.back, False), daemon=True)]
        for thread in self.threads:
            thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.front.close()
        self.back.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hold UDP packets for a set time in each direction, to test latency tools.")
    parser.add_argument("--listen", type=int, required=True, help="port the sender should be pointed at")
    parser.add_argument("--to", required=True, metavar="HOST:PORT", help="where the reflector is listening")
    parser.add_argument("--out-ms", type=float, default=0.0, help="delay on the way to the reflector")
    parser.add_argument("--back-ms", type=float, default=0.0, help="delay on the way back")
    parser.add_argument("--out-loss", type=float, default=0.0, help="share of packets to lose on the way out, 0 to 1")
    parser.add_argument("--back-loss", type=float, default=0.0, help="share of packets to lose on the way back, 0 to 1")
    args = parser.parse_args(argv)
    host, _, port = args.to.rpartition(":")
    relay = Relay(args.listen, (host or "127.0.0.1", int(port)), args.out_ms, args.back_ms, args.out_loss, args.back_loss).start()
    print(f"relaying UDP {relay.port} -> {args.to}: {args.out_ms:g} ms out, {args.back_ms:g} ms back; Ctrl+C to stop", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        relay.stop()
        print(f"out {relay.counts['out']} ({relay.counts['out_lost']} lost), back {relay.counts['back']} ({relay.counts['back_lost']} lost)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
