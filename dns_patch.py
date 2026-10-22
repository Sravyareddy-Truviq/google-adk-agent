"""
dns_patch.py — WSL / ISP DNS fallback patch.

Import this at the top of any service BEFORE other imports that touch the network.
When the system resolver (e.g. WSL's 10.255.255.254) fails, it transparently
retries via Google Public DNS (8.8.8.8) using a raw DNS-over-UDP query.

Works with: httpx, supabase-py, requests, aiohttp, websockets — anything
that goes through Python's socket layer.
"""

import socket
import struct
import os

# Public DNS servers to try when system resolver fails
_FALLBACK_DNS = [
    "8.8.8.8",   # Google
    "1.1.1.1",   # Cloudflare
]
_DNS_PORT = 53
_DNS_TIMEOUT = 3.0

# Known-good IP cache — avoids repeated fallback queries
_cache: dict[str, list[str]] = {}

# Build a minimal DNS A-record query packet
def _build_query(hostname: str, qid: int = 1) -> bytes:
    name_bytes = b"".join(
        bytes([len(p)]) + p.encode() for p in hostname.rstrip(".").split(".")
    ) + b"\x00"
    # ID + flags (standard query) + QDCOUNT=1 + ANCOUNT/NSCOUNT/ARCOUNT=0
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    question = name_bytes + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
    return header + question


def _resolve_via_udp(hostname: str, dns_server: str) -> list[str]:
    """Return list of IPv4 strings resolved via raw UDP DNS query."""
    pkt = _build_query(hostname)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(_DNS_TIMEOUT)
        s.sendto(pkt, (dns_server, _DNS_PORT))
        data, _ = s.recvfrom(512)

    # Parse answer section
    qdcount = struct.unpack(">H", data[4:6])[0]
    ancount = struct.unpack(">H", data[6:8])[0]
    if ancount == 0:
        return []

    # Skip header (12 bytes) + question section
    pos = 12
    for _ in range(qdcount):
        while data[pos] != 0:
            if data[pos] & 0xC0 == 0xC0:  # pointer
                pos += 2
                break
            pos += data[pos] + 1
        else:
            pos += 1
        pos += 4  # QTYPE + QCLASS

    ips: list[str] = []
    for _ in range(ancount):
        # Skip name (may be pointer)
        if data[pos] & 0xC0 == 0xC0:
            pos += 2
        else:
            while data[pos] != 0:
                pos += data[pos] + 1
            pos += 1
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4:  # A record
            ips.append(socket.inet_ntoa(data[pos:pos + 4]))
        pos += rdlen
    return ips


# Save the original resolver so we can call it first
_original_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    # Non-hostname lookups (IPs, None) — pass straight through
    if not isinstance(host, str) or not host or host[0].isdigit():
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    # 1. Try system resolver first (fast path)
    try:
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    except (socket.gaierror, OSError):
        pass  # fall through to DNS patch

    # 2. Check in-process cache
    if host in _cache:
        ips = _cache[host]
    else:
        # 3. Try each fallback DNS server
        ips = []
        for dns in _FALLBACK_DNS:
            try:
                ips = _resolve_via_udp(host, dns)
                if ips:
                    _cache[host] = ips
                    break
            except Exception:
                continue

    if not ips:
        raise socket.gaierror(f"[dns_patch] Could not resolve {host!r} via system or fallback DNS")

    # Build getaddrinfo-style result tuples using resolved IPs
    results = []
    for ip in ips:
        try:
            results.extend(_original_getaddrinfo(ip, port, family, type, proto, flags))
        except Exception:
            pass
    if results:
        return results
    raise socket.gaierror(f"[dns_patch] Resolved {host!r} → {ips} but getaddrinfo failed")


def apply():
    """Monkey-patch socket.getaddrinfo with fallback DNS resolver."""
    if socket.getaddrinfo is not _patched_getaddrinfo:
        socket.getaddrinfo = _patched_getaddrinfo
        print(f"[dns_patch] Applied — fallback DNS: {_FALLBACK_DNS}", flush=True)


# Auto-apply when the env var is set (useful for Render / prod where DNS is reliable
# and you might want to skip the patch)
if os.environ.get("DNS_PATCH_DISABLE") != "1":
    apply()
