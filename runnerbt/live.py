"""Live entry decisions: the backtest engine + a strategy, fed by the socket.

This is the piece the app's "new launches" screen plugs into:

    decider = LiveDecider(Strategy.load("strategies/default.json"), on_decision=handle)
    decider.process(event)   # for every event from the `stream` socket channel

`handle(decision)` receives a dict per checkpoint evaluation with the verdict,
the reason when rejected, the model score and the full feature snapshot.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import deque
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
        self.live_start: Optional[int] = None   # block time of the first live event
        self.skipped_old = 0                     # launched before we were live: never decided
        self.skipped_late = 0                    # checkpoint evaluated too late to be honest
        self.max_late_s = 15
        self._lags: deque = deque(maxlen=500)   # wall clock - block time of recent events
        cfg = engine_cfg or EngineConfig(checkpoints=tuple(strategy.checkpoints), horizon_s=horizon_s)
        self.engine = ReplayEngine(cfg, on_snapshot=self._on_snapshot)
        self.engine.on_finish = self.decided.discard

    def process(self, ev: dict):
        ts = ev.get("timestamp")
        if not self.silent and ts:
            if ts > 1e11:
                ts //= 1000
            if self.live_start is None:
                self.live_start = int(ts)
            self._lags.append(time.time() - ts)
        self.engine.process(ev)

    def status(self) -> str:
        return (f"buys {self.buys}, tracking {len(self.engine.tokens):,} launches, stream lag {self.lag():.1f}s, "
                f"skipped {self.skipped_old:,} launched before start / {self.skipped_late} late")

    def lag(self) -> float:
        """How far the stream runs behind the wall clock (90th percentile, seconds)."""
        if not self._lags:
            return 2.0
        xs = sorted(self._lags)
        return max(0.0, xs[int(len(xs) * 0.9) - 1 if len(xs) > 1 else 0])

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
            # catch up to the present now, while nothing is connected: otherwise the first live
            # event jumps the clock over the archive gap and finalises thousands of tokens at once,
            # stalling the socket long enough for the server to drop it
            if n:
                self.engine.advance(int(time.time()) - 5)
        finally:
            self.silent = False
        self.decided.clear()
        return n

    def tick(self, now: Optional[float] = None):
        """Fire due checkpoints even when the market is quiet (call every second or so).

        Uses the wall clock minus the stream's delay (plus 1s), so a checkpoint never fires
        before the trades that happened up to it have arrived - the backtest sees them all."""
        if self.live_start is None:
            return
        t = (now or time.time()) - self.lag() - 1.0
        if t > self.engine.clock:
            self.engine.advance(int(t))

    def _on_snapshot(self, st: TokenState, cp: int, feat: dict):
        if self.silent or st.mint in self.decided or cp not in self.strategy.checkpoints:
            return
        if self.live_start is None or st.created_ts < self.live_start:
            # launched during warm-up or before we connected: we missed its start, so its
            # snapshot is not comparable with the backtest
            self.skipped_old += 1
            self.decided.add(st.mint)
            return
        late = self.engine.clock - (st.created_ts + cp)
        if late > self.max_late_s:
            self.skipped_late += 1
            self.decided.add(st.mint)
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
                "quote_mint": st.quote_mint, "decided_at": time.time(), "created_ts": st.created_ts,
                "raw": {"create": st.create_ev, "last_trade": st.last_trade_ev},
                "enter": d["enter"],
                "reason": d["reason"], "score": d["score"], "features": feat,
                "strategy": self.strategy.name, "exit_plan": self.strategy.exit,
            })


def run_socket(decider: LiveDecider, protocols=None, actions=None, url: str = STREAM_URL, debug: bool = False):
    """Connect to the Advanced Data Stream and drive a LiveDecider (blocking)."""
    from .stream import run_stream

    run_stream(decider.process, protocols, actions, url, on_tick=decider.tick, tick_s=1.0,
               status_every_s=60.0, debug=debug, status_extra=decider.status)


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

    FIELDS = ["time_local", "time_utc", "unix", "mint", "symbol", "name", "protocol", "launch_utc",
              "age_s", "mcap_usd", "mcap_sol", "mcap_quote", "quote", "price_quote", "score", "strategy"]

    def __init__(self, path: str, sol_price: Optional[SolPrice] = None):
        import os
        self.path = path
        self.sol = sol_price
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, encoding="utf-8") as fh:
                header = fh.readline().strip().split(",")
            if header != self.FIELDS:  # written by an older version: keep it, start a new file
                old = os.path.splitext(path)[0] + ".old.csv"
                os.replace(path, old)
                print(f"{path} had different columns; moved it to {old}", file=sys.stderr)
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
        q = d.get("quote_mint")
        mcap_sol = mcq if q == SOL else None
        if q in STABLES:
            mcap_usd = mcq
        elif q == SOL and mcq is not None and self.sol and self.sol.usd:
            mcap_usd = mcq * self.sol.usd
        else:
            mcap_usd = None  # unknown quote asset: never guess (a USD curve read as SOL is ~200x off)
        created = d.get("created_ts")
        row = {
            "time_local": datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
            "time_utc": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "unix": int(t), "mint": d["mint"], "symbol": d.get("symbol") or "", "name": d.get("name") or "",
            "protocol": d.get("protocol") or "",
            "launch_utc": datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if created else "",
            "age_s": round(t - created, 1) if created else "",
            "mcap_usd": round(mcap_usd, 2) if mcap_usd is not None else "",
            "mcap_sol": round(mcap_sol, 4) if mcap_sol is not None else "",
            "mcap_quote": round(mcq, 4) if mcq is not None else "",
            "price_quote": d.get("price") or "",
            "quote": "SOL" if q == SOL else ("USD" if q in STABLES else (q or "unknown")),
            "score": round(d["score"], 4) if d.get("score") is not None else "", "strategy": d.get("strategy"),
        }
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=self.FIELDS).writerow(row)
        # the raw launch + last trade events, to check any number against the source
        raw_path = os.path.splitext(self.path)[0] + "_raw.jsonl"
        with open(raw_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"mint": d["mint"], "time_utc": row["time_utc"], "features": d.get("features"),
                                 **(d.get("raw") or {})}, default=str) + "\n")
        mc = f"${row['mcap_usd']:,.0f}" if row["mcap_usd"] != "" else (
            f"{row['mcap_sol']:,.1f} SOL" if row["mcap_sol"] != "" else
            f"{row['mcap_quote']} {row['quote']}")
        print(f"BUY  {row['time_local'][11:19]}  {d['mint']}  {row['symbol'] or '':<10} mcap {mc}  "
              f"({row['protocol']}, {row['age_s']}s after launch"
              f"{', score ' + str(row['score']) if row['score'] != '' else ''})", flush=True)


def print_decision(d: dict):
    out = {k: d[k] for k in ("mint", "symbol", "protocol", "checkpoint", "enter", "reason", "score", "price")}
    print(json.dumps(out), flush=True)
