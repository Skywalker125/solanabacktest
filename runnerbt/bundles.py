"""How bundling relates to runners in your data, and the bundle numbers of logged buys."""

from __future__ import annotations

import json
import os

from .hunt import rows_for

METRICS = ["early_slots_pct", "bundle_slot_pct", "max_slot_buyers", "top10_hold_pct", "dev_hold_pct",
           "unbought_sell_pct", "off_curve_pct", "launch_mcap", "launch_block_pct", "bundle_buyers", "holders"]
# metrics where a higher value means "more bundled" (holders is the opposite)
HIGHER_IS_WORSE = {m: m != "holders" for m in METRICS}


def bundle_report(records, cp: int, position: float = 0.5, slippage_pct: float = 1.0) -> str:
    feats, ys, _ = rows_for(records, cp, position=position, slippage_pct=slippage_pct)
    n, runners = len(ys), sum(ys)
    if not n:
        return f"no tradable launches at {cp}s"
    base = runners / n
    lines = [f"{n:,} launches tradable at {cp}s, {runners:,} went 5x+ ({base:.2%}).",
             "For each limit: launches kept, runners kept, and the 5x rate among the kept ones.", ""]
    for m in METRICS:
        vals = sorted(f.get(m) for f in feats if f.get(m) is not None)
        if not vals:
            lines.append(f"{m}: not in this dataset (rebuild it)")
            continue
        worse = HIGHER_IS_WORSE[m]
        lines.append(f"{m}  (median {vals[len(vals) // 2]:.3g}, 90th pct {vals[int(len(vals) * 0.9)]:.3g})")
        cuts = sorted({vals[min(len(vals) - 1, int(q * len(vals)))] for q in (0.25, 0.5, 0.75, 0.9, 0.95)})
        for c in cuts:
            keep = [y for f, y in zip(feats, ys) if f.get(m) is not None and
                    ((f[m] <= c) if worse else (f[m] >= c))]
            k, r = len(keep), sum(keep)
            op = "<=" if worse else ">="
            rate = r / k if k else 0.0
            lines.append(f"   {m} {op} {c:<10.4g} keeps {k / n:6.1%} of launches, {r / runners if runners else 0:6.1%} "
                         f"of runners, 5x rate {rate:6.2%} ({rate / base if base else 0:4.1f}x base)")
        lines.append("")
    lines.append("A good limit keeps most runners while dropping many launches (5x rate above base).")
    return "\n".join(lines)


def buys_report(path: str) -> str:
    raw = os.path.splitext(path)[0] + "_raw.jsonl"
    if not os.path.exists(raw):
        return f"no {raw} yet (it is written by live for every buy)"
    lines = [f"{'time (utc)':<20} {'mint':<45} " + " ".join(f"{m[:12]:>12}" for m in METRICS)]
    with open(raw, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            f = d.get("features") or {}
            cells = []
            for m in METRICS:
                v = f.get(m)
                cells.append(f"{v:>12.3g}" if isinstance(v, (int, float)) else f"{'-':>12}")
            lines.append(f"{d.get('time_utc', ''):<20} {d['mint']:<45} " + " ".join(cells))
    return "\n".join(lines)
