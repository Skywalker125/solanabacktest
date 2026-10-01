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
    """Per launchpad + quote: create counts, and launch market cap as reported vs. computed
    from the first trade's executed amounts (quoteAmount / tokenAmount x supply)."""
    files = list_event_files(paths)[::-1]
    creates: dict = {}
    reported: dict = {}
    computed: dict = {}
    seen: dict = {}
    n_creates = 0
    for f in files:
        for ev in iter_file(f):
            a = ev.get("action")
            if a == "create":
                n_creates += 1
                key = (ev.get("protocol"), ev.get("quoteMint") or "<missing>")
                creates[key] = creates.get(key, 0) + 1
                seen[ev.get("mint")] = (ev.get("protocol"), float(ev.get("supply") or 1e9))
            elif a in ("buy", "sell") and ev.get("mint") in seen:
                proto, supply = seen.pop(ev["mint"])
                key = (proto, ev.get("quoteMint") or "<missing>")
                if ev.get("marketCapQuote") is not None:
                    reported.setdefault(key, []).append(float(ev["marketCapQuote"]))
                ta, qa = ev.get("tokenAmount"), ev.get("quoteAmount")
                if ta and qa and float(ta) > 0 and len(ev.get("breakdown") or []) <= 1:
                    computed.setdefault(key, []).append(float(qa) / float(ta) * supply)
        if n_creates >= max_creates:
            break
    names = {"So11111111111111111111111111111111111111112": "SOL",
             "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
             "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB": "USD1"}
    total = sum(creates.values()) or 1
    sol = sum(c for (p, q), c in creates.items() if q == "So11111111111111111111111111111111111111112")
    lines = [f"{n_creates:,} launches scanned; {sol / total:.0%} quoted in SOL (the backtest uses only those "
             f"unless build --all-quotes)", "",
             f"{'protocol':<16} {'quote':<46} {'launches':>8} {'reported mcap':>16} {'computed mcap':>16}"]
    keys = sorted(creates, key=lambda k: -creates[k])[:25]
    for k in keys:
        rep = f"{statistics.median(reported[k]):,.2f}" if k in reported else "-"
        com = f"{statistics.median(computed[k]):,.2f}" if k in computed else "-"
        lines.append(f"{str(k[0]):<16} {names.get(k[1], k[1]):<46} {creates[k]:>8} {rep:>16} {com:>16}")
    lines.append("\n(medians at the first trade, in units of the quote; a SOL launch starts around 25-35 SOL. "
                 "The backtest uses the computed value.)")
    return "\n".join(lines)


def dump(events) -> str:
    return "\n".join(json.dumps(e, default=str) for e in events)
