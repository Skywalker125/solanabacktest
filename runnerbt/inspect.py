"""Look inside archive files: raw events of one token, or what each launchpad reports."""

from __future__ import annotations

import json
import statistics
from typing import Optional

from .io import iter_file, list_event_files


def find_events(paths, mint: Optional[str] = None, protocol: Optional[str] = None,
                action: Optional[str] = None, limit: int = 20, newest_first: bool = True):
    files = list_event_files(paths)
    if newest_first:
        files = files[::-1]
    n = 0
    for f in files:
        for ev in iter_file(f):
            if mint and ev.get("mint") != mint:
                continue
            if protocol and ev.get("protocol") != protocol:
                continue
            if action and ev.get("action") != action:
                continue
            yield ev
            n += 1
            if n >= limit:
                return


def quote_report(paths, max_creates: int = 20000) -> str:
    """Per launchpad: which quote mints creates/trades name, and the market cap at launch."""
    files = list_event_files(paths)[::-1]
    creates: dict = {}
    first_mc: dict = {}
    seen: dict = {}
    n_creates = 0
    for f in files:
        for ev in iter_file(f):
            a = ev.get("action")
            if a == "create":
                n_creates += 1
                key = (ev.get("protocol"), ev.get("quoteMint") or "<missing>")
                creates[key] = creates.get(key, 0) + 1
                seen[ev.get("mint")] = ev.get("protocol")
            elif a in ("buy", "sell") and ev.get("mint") in seen and ev.get("marketCapQuote") is not None:
                key = (seen.pop(ev["mint"]), ev.get("quoteMint") or "<missing>")
                first_mc.setdefault(key, []).append(float(ev["marketCapQuote"]))
        if n_creates >= max_creates:
            break
    lines = [f"{'protocol':<16} {'quoteMint on create':<46} {'creates':>8}"]
    for (p, q), c in sorted(creates.items(), key=lambda kv: -kv[1]):
        lines.append(f"{str(p):<16} {q:<46} {c:>8}")
    lines.append("")
    lines.append(f"{'protocol':<16} {'quoteMint on first trade':<46} {'n':>6} {'median marketCapQuote':>22}")
    for (p, q), xs in sorted(first_mc.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"{str(p):<16} {q:<46} {len(xs):>6} {statistics.median(xs):>22,.2f}")
    lines.append("\nA SOL-quoted launch starts around 25-35 marketCapQuote; values in the thousands mean the "
                 "curve is quoted in USD (or another token) whatever quoteMint says.")
    return "\n".join(lines)


def dump(events) -> str:
    return "\n".join(json.dumps(e, default=str) for e in events)
