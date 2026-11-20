#!/usr/bin/env python3
"""Find out who else claims a .local name, without trusting our own records.

Two independent methods, because each fails where the other works:

  multicast  one query to 224.0.0.251, collect every responder. Needs no ARP
             entry, so it is the one that works on a box that just booted.
  unicast    ask known hosts directly on port 5353. Works where the network
             does not carry multicast between ports.

Responders are matched by IP and our own addresses are filtered out, so our
own avahi answering its own record is ignored. That is the trap
`avahi-resolve` cannot escape: on a box publishing the name, it always
resolves to itself and an incumbent peer stays invisible.

Both were needed in practice. ARP-only discovery reported "nobody claims it"
on a freshly booted board whose neighbour table was still empty, and the
caller reclaimed a name straight back into a collision.

    mdns_probe.py safydo                 # multicast + ARP neighbours
    mdns_probe.py safydo 10.0.0.5 ...    # only these hosts, unicast

Prints one "<ip> <responder>" line per claimant. Silence means nobody else
answers — which is weaker than proof that the name is free.
"""

from __future__ import annotations

import ipaddress
import socket
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
TIMEOUT = 1.5
# A sweep is 250-odd hosts on the local wire, where a live responder answers in
# milliseconds. Keeping this short bounds the whole no-hit case, which runs at
# boot ahead of other units.
SWEEP_TIMEOUT = 0.6
MCAST_WINDOW = 1.5


def arp_neighbours() -> list[str]:
    try:
        out = subprocess.run(
            ["ip", "-4", "neigh"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    seen = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip = parts[0]
        # FAILED/INCOMPLETE entries are stale ARP noise, not live hosts.
        if "FAILED" in line or "INCOMPLETE" in line:
            continue
        if ip.count(".") == 3 and ip not in seen:
            seen.append(ip)
    return seen


def own_ips() -> set[str]:
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {ln.split()[3].split("/")[0] for ln in out.splitlines() if len(ln.split()) > 3}


def build_query(name: str) -> bytes:
    """A standard DNS A query, with the mDNS QU bit set so the peer replies
    straight back to our port instead of multicasting the answer."""
    header = struct.pack(">HHHHHH", 0x4242, 0x0000, 1, 0, 0, 0)
    qname = b"".join(
        bytes([len(p)]) + p.encode("ascii") for p in name.split(".") if p
    ) + b"\x00"
    return header + qname + struct.pack(">HH", 1, 0x8001)  # QTYPE=A, QU|IN


def _skip_name(data: bytes, off: int) -> int:
    while off < len(data):
        ln = data[off]
        if ln == 0:
            return off + 1
        if ln & 0xC0 == 0xC0:      # compression pointer: 2 bytes, ends the name
            return off + 2
        off += 1 + ln
    return off


def parse_a_records(data: bytes) -> list[str]:
    if len(data) < 12:
        return []
    _, _, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
    off = 12
    for _ in range(qd):
        off = _skip_name(data, off) + 4
    out = []
    for _ in range(an):
        off = _skip_name(data, off)
        if off + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        if rtype == 1 and rdlen == 4 and off + 4 <= len(data):
            out.append(socket.inet_ntoa(data[off:off + 4]))
        off += rdlen
    return out


def probe_multicast(name: str, mine: set[str]) -> list[tuple[str, str]]:
    """Ask the whole segment at once and collect every responder.

    This is the reliable half: it needs no ARP entry, so it works on a box
    that just booted, and because responders are matched by IP our own avahi
    answering its own record is simply filtered out — which is the trap that
    `avahi-resolve` cannot escape.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            s.settimeout(0.4)
            s.sendto(build_query(name), (MDNS_GROUP, MDNS_PORT))
            deadline = time.monotonic() + MCAST_WINDOW
            while time.monotonic() < deadline:
                try:
                    data, addr = s.recvfrom(4096)
                except (socket.timeout, OSError):
                    continue
                for ip in parse_a_records(data):
                    if ip not in mine and ip not in seen:
                        seen.add(ip)
                        found.append((ip, addr[0]))
    except OSError:
        pass
    return found


def local_subnet_hosts(limit: int = 512) -> list[str]:
    """Every address on our own IPv4 /24-ish, for a parallel unicast sweep.

    The ARP table is the fast path but it is empty on a freshly booted box, and
    a broadcast ping does not fill it because Linux ignores broadcast ICMP by
    default (net.ipv4.icmp_echo_ignore_broadcasts=1). Sweeping is the only way
    to find a peer when multicast is also filtered.
    """
    out: list[str] = []
    try:
        raw = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            net = ipaddress.ip_network(parts[3], strict=False)
        except ValueError:
            continue
        if net.num_addresses > limit:      # do not sweep a /16
            continue
        out.extend(str(h) for h in net.hosts())
    return out


def probe(host: str, name: str, timeout: float = TIMEOUT) -> list[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(build_query(name), (host, MDNS_PORT))
            data, _ = s.recvfrom(4096)
        return parse_a_records(data)
    except (OSError, socket.timeout, struct.error):
        return []


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    base = sys.argv[1]
    fqdn = base if base.endswith(".local") else f"{base}.local"
    mine = own_ips()
    explicit = sys.argv[2:]

    # Multicast first: no ARP dependency, so it works on a freshly booted box.
    reported: set[str] = set()
    if not explicit:
        for ip, via in probe_multicast(fqdn, mine):
            reported.add(ip)
            print(f"{ip} {via}")

    peers = explicit or arp_neighbours()
    if not explicit and not reported:
        # Multicast found nobody: it may simply not be forwarded here. Fall
        # back to sweeping the local subnet. Parallel, so ~250 hosts cost about
        # one timeout in wall-clock rather than 250 of them.
        peers = list(dict.fromkeys(peers + local_subnet_hosts()))

    targets = [h for h in peers if h not in mine]
    if not targets:
        return 0
    per_host = TIMEOUT if explicit else SWEEP_TIMEOUT
    with ThreadPoolExecutor(max_workers=128) as pool:
        for host, ips in zip(targets, pool.map(lambda h: probe(h, fqdn, per_host), targets)):
            for ip in ips:
                if ip not in mine and ip not in reported:
                    reported.add(ip)
                    print(f"{ip} {host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
