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


def mcap_check(paths, max_launches: int = 5000) -> str:
    """Per launchpad, at each launch's first trades: market cap as reported by the stream, as implied
    by the executed amounts (quoteAmount / tokenAmount x supply), and as computed by the engine.
    If the engine column is ~10x off the amounts column, the price-unit correction misfires."""
    from .engine import EngineConfig, ReplayEngine
    from .io import iter_events
    rows: dict = {}
    done: set = set()

    def on_trade(st, ev):
        if st.mint in done or st.last_mcap is None:
            return
        ta, qa = ev.get("tokenAmount"), ev.get("quoteAmount")
        if not (ta and qa and float(ta) > 0 and len(ev.get("breakdown") or []) <= 1):
            return
        supply = float(st.supply or 1e9)
        r = rows.setdefault(st.protocol, {"reported": [], "amounts": [], "engine": [], "scale": [], "supply": []})
        if ev.get("marketCapQuote") is not None:
            r["reported"].append(float(ev["marketCapQuote"]))
        r["amounts"].append(float(qa) / float(ta) * supply)
        r["engine"].append(st.last_mcap)
        r["scale"].append(st.price_scale or 1.0)
        r["supply"].append(supply)
        done.add(st.mint)

    eng = ReplayEngine(EngineConfig(checkpoints=(30,), horizon_s=600), on_trade=on_trade)
    for ev in iter_events(paths):
        eng.process(ev)
        if len(done) >= max_launches:
            break
    med = lambda xs: f"{statistics.median(xs):,.2f}" if xs else "-"  # noqa: E731
    lines = [f"{'protocol':<16}{'launches':>9}{'reported':>14}{'from amounts':>14}{'engine':>12}"
             f"{'price scale':>13}  {'supply':>18}"]
    for p, r in sorted(rows.items(), key=lambda kv: -len(kv[1]["amounts"])):
        lines.append(f"{str(p):<16}{len(r['amounts']):>9}{med(r['reported']):>14}{med(r['amounts']):>14}"
                     f"{med(r['engine']):>12}{med(r['scale']):>13}  {med(r['supply']):>18}")
    lines.append("\nMedians of the first clean trade of each SOL-quoted launch, in SOL. 'engine' should match "
                 "'from amounts' (a SOL launch starts around 25-35 SOL). Market cap = price x supply.")
    return "\n".join(lines)
