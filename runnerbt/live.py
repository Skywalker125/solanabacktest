"""Live entry decisions: the backtest engine + a strategy, fed by the socket.

This is the piece the app's "new launches" screen plugs into:

    decider = LiveDecider(Strategy.load("strategies/default.json"), on_decision=handle)
    decider.process(event)   # for every event from the `stream` socket channel

`handle(decision)` receives a dict per checkpoint evaluation with the verdict,
the reason when rejected, the model score and the full feature snapshot.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Callable, Optional

from .engine import EngineConfig, ReplayEngine, TokenState
from .strategy import Strategy

from .stream import STREAM_URL


class LiveDecider:
    def __init__(self, strategy: Strategy, on_decision: Callable[[dict], None],
                 horizon_s: int = 6 * 3600, report_rejects: bool = False, engine_cfg: Optional[EngineConfig] = None):
        self.strategy = strategy
        self.on_decision = on_decision
        self.report_rejects = report_rejects
        self.decided: set = set()
        self.silent = False
        self.buys = 0
        cfg = engine_cfg or EngineConfig(checkpoints=tuple(strategy.checkpoints), horizon_s=horizon_s)
        self.engine = ReplayEngine(cfg, on_snapshot=self._on_snapshot,
                                   on_record=lambda r: self.decided.discard(r["mint"]))

    def process(self, ev: dict):
        self.engine.process(ev)

    def warmup(self, paths) -> int:
        """Replay archived events without deciding, so history-based features (a creator's
        earlier launches and runners) start from the same footing as in the backtest."""
        from .io import iter_events
        self.silent = True
        n = 0
        try:
            for ev in iter_events(paths):
                self.engine.process(ev)
                n += 1
        finally:
            self.silent = False
        self.decided.clear()
        return n

    def tick(self, now: Optional[int] = None):
        """Fire due checkpoints even when the market is quiet (call every second or so)."""
        self.engine.advance(int(now or time.time()))

    def _on_snapshot(self, st: TokenState, cp: int, feat: dict):
        if self.silent or st.mint in self.decided or cp not in self.strategy.checkpoints:
            return
        d = self.strategy.decide(feat)
        last_cp = cp == self.strategy.checkpoints[-1]
        if d["enter"] or last_cp:
            self.decided.add(st.mint)
        if d["enter"]:
            self.buys += 1
        if d["enter"] or self.report_rejects:
            self.on_decision({
                "mint": st.mint, "symbol": st.symbol, "name": st.name, "protocol": st.protocol,
                "checkpoint": cp, "price": st.last_price, "mcap_quote": st.last_mcap,
                "quote_mint": st.quote_mint, "decided_at": time.time(), "enter": d["enter"],
                "reason": d["reason"], "score": d["score"], "features": feat,
                "strategy": self.strategy.name, "exit_plan": self.strategy.exit,
            })


def run_socket(decider: LiveDecider, protocols=None, actions=None, url: str = STREAM_URL, debug: bool = False):
    """Connect to the Advanced Data Stream and drive a LiveDecider (blocking)."""
    from .stream import run_stream

    run_stream(decider.process, protocols, actions, url, on_tick=decider.tick, tick_s=1.0,
               status_every_s=60.0, debug=debug)


class SolPrice:
    """SOL/USD in the background (Jupiter, then CoinGecko) so market caps can be logged in USD."""

    URLS = (("https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112", "jupiter"),
            ("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd", "coingecko"))

    def __init__(self, refresh_s: float = 60):
        import threading
        self.usd: Optional[float] = None
        self.refresh_s = refresh_s
        threading.Thread(target=self._run, daemon=True).start()

    def _fetch(self) -> Optional[float]:
        import urllib.request
        for url, name in self.URLS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "runnerbt"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.loads(r.read())
                if name == "jupiter":
                    e = d.get("So11111111111111111111111111111111111111112") or {}
                    v = e.get("usdPrice") or e.get("price")
                else:
                    v = d["solana"]["usd"]
                if v:
                    return float(v)
            except Exception:
                continue
        return None

    def _run(self):
        while True:
            v = self._fetch()
            if v:
                self.usd = v
            time.sleep(self.refresh_s)


STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
           "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"}
SOL = "So11111111111111111111111111111111111111112"


class BuyLog:
    """Appends every fired buy to a CSV (one row per token)."""

    FIELDS = ["time_utc", "unix", "mint", "symbol", "name", "protocol", "decided_after_s",
              "mcap_usd", "mcap_sol", "price_quote", "quote", "score", "strategy"]

    def __init__(self, path: str, sol_price: Optional[SolPrice] = None):
        import os
        self.path = path
        self.sol = sol_price
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                import csv
                csv.writer(fh).writerow(self.FIELDS)

    def __call__(self, d: dict):
        import csv
        from datetime import datetime, timezone
        if not d.get("enter"):
            print_decision(d)
            return
        t = d.get("decided_at") or time.time()
        mcq = d.get("mcap_quote")
        q = d.get("quote_mint") or SOL
        mcap_sol = mcq if q == SOL else None
        if q in STABLES:
            mcap_usd = mcq
        elif q == SOL and mcq is not None and self.sol and self.sol.usd:
            mcap_usd = mcq * self.sol.usd
        else:
            mcap_usd = None
        row = {
            "time_utc": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "unix": int(t), "mint": d["mint"], "symbol": d.get("symbol") or "", "name": d.get("name") or "",
            "protocol": d.get("protocol") or "", "decided_after_s": d.get("checkpoint"),
            "mcap_usd": round(mcap_usd, 2) if mcap_usd is not None else "",
            "mcap_sol": round(mcap_sol, 4) if mcap_sol is not None else "",
            "price_quote": d.get("price") or "", "quote": "SOL" if q == SOL else q,
            "score": round(d["score"], 4) if d.get("score") is not None else "", "strategy": d.get("strategy"),
        }
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=self.FIELDS).writerow(row)
        mc = f"${row['mcap_usd']:,.0f}" if row["mcap_usd"] != "" else (
            f"{row['mcap_sol']:,.1f} SOL" if row["mcap_sol"] != "" else "?")
        print(f"BUY  {row['time_utc']}  {d['mint']}  {row['symbol'] or '':<10} mcap {mc}  "
              f"({row['protocol']}, {row['decided_after_s']}s after launch"
              f"{', score ' + str(row['score']) if row['score'] != '' else ''})", flush=True)


def print_decision(d: dict):
    out = {k: d[k] for k in ("mint", "symbol", "protocol", "checkpoint", "enter", "reason", "score", "price")}
    print(json.dumps(out), flush=True)
