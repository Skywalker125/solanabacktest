"""Trade simulation and metrics over a dataset of token records."""

from __future__ import annotations

import math
import statistics
from typing import Iterable, Optional

from . import RUNNER_MULT
from .strategy import Strategy


def _buy_impact(size: float, q: Optional[float]) -> float:
    """Average-price multiplier for buying `size` quote into a constant-product pool."""
    return 1.0 + size / q if q and q > 0 else 1.0


def _sell_out(value: float, q: Optional[float]) -> float:
    """Quote received for tokens worth `value` at spot in a constant-product pool."""
    return value / (1.0 + value / q) if q and q > 0 else value


def entry_for(record: dict, cp: int):
    e = record.get("entries", {}).get(str(cp))
    return e if e and e[1] else None


def entry_impact(entry, position: float, slippage_pct: float) -> float:
    """Fill price / quoted price for a buy of `position` quote: pool impact + slippage."""
    q = entry[2] if entry and len(entry) > 2 else None
    return _buy_impact(position, q) * (1 + slippage_pct / 100.0)


def is_runner(record: dict, cp: int, position: float = 0.5, slippage_pct: float = 1.0,
              mult: float = RUNNER_MULT) -> Optional[bool]:
    """Did the price reach `mult` x our actual fill (not just the quoted price)? None if untradable."""
    lab = label(record, cp, mult)
    if not lab:
        return None
    return lab["max_mult"] / entry_impact(entry_for(record, cp), position, slippage_pct) >= mult


def label(record: dict, cp: int, mult: float = RUNNER_MULT) -> Optional[dict]:
    """Outcome after entering at checkpoint `cp` (raw prices, no costs)."""
    e = entry_for(record, cp)
    if not e:
        return None
    t0, p0 = e[0], e[1]
    end = record["created_ts"] + record["horizon_s"]
    peak, t_hit = p0, None
    for ts, p, _q in record["path"]:
        if ts < t0 or ts > end:
            continue
        if p > peak:
            peak = p
        if t_hit is None and p >= p0 * mult:
            t_hit = ts - t0
    return {"max_mult": peak / p0, "runner": peak / p0 >= mult, "time_to_target": t_hit}


def simulate_trade(record: dict, cp: int, strat: Strategy) -> Optional[dict]:
    e = entry_for(record, cp)
    if not e:
        return None
    t0, p0, q0 = e
    ex = strat.exit
    fee = strat.fee_pct / 100.0
    slip = strat.slippage_pct / 100.0
    size = strat.position

    fill = p0 * _buy_impact(size, q0) * (1 + slip)
    tokens = size * (1 - fee) / fill
    remaining = 1.0
    proceeds = 0.0
    tps = sorted(ex.get("take_profit") or [], key=lambda x: x["mult"])
    tp_i = 0
    stop = ex.get("stop_loss")
    trail = ex.get("trailing") or None
    hold_end = min(t0 + (ex.get("max_hold_s") or 10 ** 12), record["created_ts"] + record["horizon_s"])
    peak = p0
    exit_reason = "time"
    last_p, last_q, exit_ts = p0, q0, t0
    max_seen = p0

    def sell(frac_of_initial: float, price: float, q):
        nonlocal remaining, proceeds
        frac = min(frac_of_initial, remaining)
        if frac <= 0:
            return
        value = tokens * frac * price
        proceeds += _sell_out(value, q) * (1 - slip) * (1 - fee)
        remaining -= frac

    for ts, p, q in record["path"]:
        if ts <= t0:
            continue
        if ts > hold_end:
            break
        last_p, last_q, exit_ts = p, q, ts
        max_seen = max(max_seen, p)
        while tp_i < len(tps) and remaining > 1e-9 and p >= p0 * tps[tp_i]["mult"]:
            tp = tps[tp_i]
            # limit-style exit at the target; whatever is left goes on the last level
            frac = remaining if tp_i == len(tps) - 1 else tp["frac"]
            sell(frac, p0 * tp["mult"], q)
            tp_i += 1
            exit_reason = f"tp{tp['mult']:g}"
        if remaining <= 1e-9:
            break
        peak = max(peak, p)
        if stop is not None and p <= p0 * stop:
            sell(remaining, p, q)  # market exit at the observed (possibly gapped) price
            exit_reason = "stop" if tp_i == 0 else exit_reason + "+stop"
            break
        if trail and peak >= p0 * trail["activate"] and p <= peak * (1 - trail["drop"]):
            sell(remaining, p, q)
            exit_reason = "trail" if tp_i == 0 else exit_reason + "+trail"
            break
    if remaining > 1e-9:
        sell(remaining, last_p, last_q)
        if exit_reason.startswith("tp"):
            exit_reason += "+time"

    lab = label(record, cp)
    pnl = proceeds - size
    return {
        "mint": record["mint"], "symbol": record.get("symbol"), "protocol": record.get("protocol"),
        "created_ts": record["created_ts"], "checkpoint": cp, "entry_ts": t0, "exit_ts": exit_ts,
        "entry_price": p0, "fill_price": fill, "max_mult": lab["max_mult"] if lab else max_seen / p0,
        "runner": bool(lab and lab["max_mult"] * p0 / fill >= RUNNER_MULT), "exit_reason": exit_reason,
        "cost": size, "proceeds": proceeds, "pnl": pnl, "roi": pnl / size,
    }


def run_backtest(records: Iterable[dict], strat: Strategy, keep_trades: bool = True) -> dict:
    trades = []
    n_tokens = 0
    n_runners = 0  # runners reachable from the first strategy checkpoint (recall denominator)
    first_cp = strat.checkpoints[0]
    for rec in records:
        if not rec.get("complete", True):
            continue
        n_tokens += 1
        if is_runner(rec, first_cp, strat.position, strat.slippage_pct):
            n_runners += 1
        for cp in strat.checkpoints:
            feat = rec.get("snapshots", {}).get(str(cp))
            if not feat:
                continue
            d = strat.decide(feat)
            if d["enter"]:
                t = simulate_trade(rec, cp, strat)
                if t:
                    t["score"] = d["score"]
                    trades.append(t)
                break
    trades.sort(key=lambda t: t["entry_ts"])
    m = metrics(trades, n_tokens, n_runners)
    if keep_trades:
        m["trade_list"] = trades
    return m


def metrics(trades: list, n_tokens: int, n_runners: int) -> dict:
    n = len(trades)
    pnl = [t["pnl"] for t in trades]
    rois = [t["roi"] for t in trades]
    caught = sum(1 for t in trades if t["runner"])
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    equity, peak, mdd = 0.0, 0.0, 0.0
    for x in pnl:
        equity += x
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)
    by_proto: dict = {}
    for t in trades:
        b = by_proto.setdefault(t["protocol"], {"trades": 0, "runners": 0, "pnl": 0.0})
        b["trades"] += 1
        b["runners"] += int(t["runner"])
        b["pnl"] += t["pnl"]
    exits: dict = {}
    for t in trades:
        exits[t["exit_reason"]] = exits.get(t["exit_reason"], 0) + 1
    invested = sum(t["cost"] for t in trades)
    return {
        "tokens": n_tokens,
        "runners_available": n_runners,
        "base_rate": n_runners / n_tokens if n_tokens else 0.0,
        "trades": n,
        "runners_caught": caught,
        "precision": caught / n if n else 0.0,
        "recall": caught / n_runners if n_runners else 0.0,
        "lift": (caught / n) / (n_runners / n_tokens) if n and n_runners else 0.0,
        "total_pnl": sum(pnl),
        "invested": invested,
        "roi": sum(pnl) / invested if invested else 0.0,
        "avg_roi": statistics.fmean(rois) if rois else 0.0,
        "median_roi": statistics.median(rois) if rois else 0.0,
        "win_rate": len(wins) / n if n else 0.0,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else math.inf if wins else 0.0,
        "max_drawdown": mdd,
        "best_trade": max(pnl) if pnl else 0.0,
        "by_protocol": by_proto,
        "exit_reasons": exits,
    }


def format_report(m: dict, title: str = "Backtest") -> str:
    lines = [
        f"== {title} ==",
        f"tokens followed      {m['tokens']:>10}",
        f"5x+ runners          {m['runners_available']:>10}   base rate {m['base_rate']:.2%}",
        f"entries taken        {m['trades']:>10}",
        f"runners caught       {m['runners_caught']:>10}   precision {m['precision']:.2%}  "
        f"recall {m['recall']:.2%}  lift {m['lift']:.1f}x",
        f"win rate             {m['win_rate']:>10.2%}",
        f"total pnl (quote)    {m['total_pnl']:>10.3f}   on {m['invested']:.2f} invested  roi {m['roi']:.2%}",
        f"avg / median roi     {m['avg_roi']:>10.2%} / {m['median_roi']:.2%}",
        f"profit factor        {m['profit_factor']:>10.2f}",
        f"max drawdown         {m['max_drawdown']:>10.3f}",
        f"best trade           {m['best_trade']:>10.3f}",
    ]
    if m["by_protocol"]:
        lines.append("by protocol:")
        for k, v in sorted(m["by_protocol"].items(), key=lambda kv: -kv[1]["trades"]):
            lines.append(f"  {str(k):<16} trades {v['trades']:>6}  runners {v['runners']:>5}  pnl {v['pnl']:>9.3f}")
    if m["exit_reasons"]:
        lines.append("exits: " + ", ".join(f"{k}={v}" for k, v in sorted(m["exit_reasons"].items(), key=lambda kv: -kv[1])))
    return "\n".join(lines)
