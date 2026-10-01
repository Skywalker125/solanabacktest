"""Random search over entry filters and exits, validated out-of-sample.

Thresholds are sampled from the empirical quantiles of each feature, the
score is computed on the chronologically earlier part of the data, and the
best candidates are re-scored on the later part so overfit rules show up.
"""

from __future__ import annotations

import copy
import random

from .simulate import run_backtest
from .strategy import Strategy

# feature -> which side of the threshold we search ("min", "max")
SEARCH_SPACE = {
    "n_buys": "min", "unique_buyers": "min", "buy_vol": "min", "net_flow": "min",
    "flow_30s": "min", "buys_30s": "min", "buy_sell_ratio": "min", "mult_from_launch": "min",
    "mcap": "max", "top3_share": "max", "top1_share": "max", "bundle_share": "max",
    "dev_initial_pct": "max", "dev_sold_pct": "max", "creator_prev_launches": "max",
    "creator_open_launches": "max", "drawdown_from_max": "min",
}
EXIT_SPACE = {
    "stop_loss": [None, 0.3, 0.4, 0.5, 0.6, 0.7],
    "take_profit": [
        [{"mult": 5.0, "frac": 1.0}],
        [{"mult": 3.0, "frac": 0.4}, {"mult": 5.0, "frac": 1.0}],
        [{"mult": 5.0, "frac": 0.6}, {"mult": 10.0, "frac": 1.0}],
        [{"mult": 5.0, "frac": 0.5}, {"mult": 20.0, "frac": 1.0}],
        [{"mult": 2.0, "frac": 0.5}, {"mult": 5.0, "frac": 0.3}, {"mult": 10.0, "frac": 1.0}],
    ],
    "trailing": [None, {"activate": 3.0, "drop": 0.4}, {"activate": 5.0, "drop": 0.35}],
}


def _quantiles(records, cp, key, qs=(0.1, 0.25, 0.4, 0.5, 0.6, 0.75, 0.85, 0.9, 0.95)):
    vals = sorted(r["snapshots"][str(cp)][key] for r in records
                  if str(cp) in r.get("snapshots", {}) and r["snapshots"][str(cp)].get(key) is not None)
    if not vals:
        return []
    return sorted({vals[min(len(vals) - 1, int(q * len(vals)))] for q in qs})


def objective(m: dict, min_trades: int) -> float:
    if m["trades"] < min_trades:
        return -1e9 + m["trades"]
    # total pnl, lightly penalised by drawdown so one lucky moonshot does not dominate
    return m["total_pnl"] - 0.25 * m["max_drawdown"]


def optimize(train, test, base: Strategy, iters: int = 500, min_trades: int = 20,
             max_filters: int = 5, search_exits: bool = True, seed: int = 7, top: int = 5,
             log=print) -> list[dict]:
    rng = random.Random(seed)
    cps = base.checkpoints
    quant = {cp: {k: _quantiles(train, cp, k) for k in SEARCH_SPACE} for cp in cps}
    results = []

    def evaluate(s):
        m = run_backtest(train, s, keep_trades=False)
        return objective(m, min_trades), m

    base_score, base_m = evaluate(base)
    results.append((base_score, base, base_m))
    for i in range(iters):
        s = copy.deepcopy(base)
        cp = rng.choice(cps)
        s.checkpoints = [cp]
        keys = rng.sample(list(SEARCH_SPACE), rng.randint(1, max_filters))
        for k in keys:
            qv = quant[cp].get(k)
            if not qv:
                continue
            s.filters[k] = {SEARCH_SPACE[k]: rng.choice(qv)}
        if search_exits:
            for k, opts in EXIT_SPACE.items():
                s.exit[k] = copy.deepcopy(rng.choice(opts))
        sc, m = evaluate(s)
        results.append((sc, s, m))
        if log and (i + 1) % max(1, iters // 10) == 0:
            best = max(results, key=lambda t: t[0])
            log(f"  {i + 1}/{iters}  best train pnl {best[2]['total_pnl']:.3f} "
                f"({best[2]['trades']} trades, {best[2]['runners_caught']} runners)")
    results.sort(key=lambda t: -t[0])
    out = []
    seen = set()
    for sc, s, m in results:
        key = repr(s.to_dict())
        if key in seen:
            continue
        seen.add(key)
        mt = run_backtest(test, s, keep_trades=False) if test else None
        out.append({"strategy": s, "train": m, "test": mt, "score": sc})
        if len(out) >= top:
            break
    return out
