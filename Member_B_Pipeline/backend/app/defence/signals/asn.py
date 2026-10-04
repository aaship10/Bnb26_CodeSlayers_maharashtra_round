"""Static IP-range -> ASN table (data/asn_table.csv). DUMMY data: documentation and private ranges with
private-use AS numbers, so no real network is described. Replace the file with a real prefix list to use it
for real; the lookup code does not change."""
from __future__ import annotations

import csv
import ipaddress
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "asn_table.csv"


@lru_cache(maxsize=1)
def _table() -> tuple[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, int, str], ...]:
    rows = []
    with DATA.open(encoding="utf-8") as f:
        for rec in csv.reader(line for line in f if line.strip() and not line.lstrip().startswith("#")):
            if rec[0] == "cidr":
                continue
            rows.append((ipaddress.ip_network(rec[0]), int(rec[1]), rec[2]))
    # longest prefix first, so the most specific range wins
    return tuple(sorted(rows, key=lambda r: r[0].prefixlen, reverse=True))


def asn_of(ip: str | None) -> tuple[int, str, str] | None:
    """(asn, cidr, name) for the most specific matching range, or None."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for net, asn, name in _table():
        if addr.version == net.version and addr in net:
            return asn, str(net), name
    return None
