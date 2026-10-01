"""Replay engine: turns the raw Advanced Data Stream into per-token records.

The same engine runs over archived events (backtest) and over the live socket
(LiveDecider), so features used for a decision are computed identically in
both places.  Everything is point-in-time: a snapshot taken at checkpoint `cp`
only sees events with timestamp <= created_ts + cp.
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from . import RUNNER_MULT, SOL_MINT

TRADE_ACTIONS = ("buy", "sell")
RISKY_EXTENSIONS = {"transferFeeConfig", "permanentDelegate", "transferHook", "defaultAccountState", "nonTransferable"}


@dataclass
class EngineConfig:
    checkpoints: tuple = (10, 30, 60, 120, 300)  # seconds after launch
    horizon_s: int = 6 * 3600                     # how long to follow each token
    path_step: float = 0.01                       # store a path point on >=1% price move
    include_pool_launches: bool = True            # treat createPool on an unseen mint as a launch
    sol_only: bool = False                        # ignore launches not quoted in SOL
    protocols: Optional[set] = None               # restrict launches to these protocols


class TokenState:
    """Mutable state for one launched token while it is being followed."""

    def __init__(self, mint: str, ev: dict, launch_type: str, creator_hist: tuple, creator_open: int = 0):
        self.mint = mint
        self.protocol = ev.get("protocol")
        self.launch_type = launch_type
        self.created_ts = ev.get("timestamp") or 0
        self.created_block = ev.get("block")
        self.signature = ev.get("signature")
        self.pools = {p for p in (ev.get("pool"),) if p}
        self.quote_mint = ev.get("quoteMint") or SOL_MINT
        self.creator = ev.get("creator") or ev.get("txSigner")
        self.name = ev.get("name")
        self.symbol = ev.get("symbol")
        self.supply = ev.get("supply")
        self.mint_authority = ev.get("mintAuthority")
        self.freeze_authority = ev.get("freezeAuthority")
        self.extensions = list(ev.get("tokenExtensions") or [])
        ib = ev.get("initialBuy") or {}
        self.dev_initial_buy_quote = float(ib.get("quoteAmount") or 0.0)
        self.dev_initial_buy_tokens = float(ib.get("tokenAmount") or 0.0)
        self.creator_prev_launches, self.creator_prev_runners = creator_hist
        self.creator_open_launches = creator_open  # same creator's tokens still being followed

        # trade aggregates (only maintained until the last checkpoint)
        self.n_buys = 0
        self.n_sells = 0
        self.buy_vol = 0.0
        self.sell_vol = 0.0
        self.buyers: dict[str, float] = {}
        self.sellers: set = set()
        self.signers: set = set()
        self.bundle_buyers: set = set()
        self.bundle_vol = 0.0
        self.dev_bought_tokens = 0.0
        self.dev_sold_tokens = 0.0
        self.dev_sell_count = 0
        self.recent: deque = deque()  # (ts, signed quote) for flow-rate features
        self.liq_removes = 0
        self.fee_claims = 0
        self.migrated = False
        self.migrate_ts: Optional[int] = None
        self.curve_complete = False

        # price state
        price0 = None
        tip, qip = ev.get("tokensInPool"), ev.get("quoteInPool")
        if tip and qip:
            price0 = float(qip) / float(tip)
        self.first_price = price0
        self.last_price = price0
        self.max_price = price0 or 0.0
        self.last_mcap = None
        self.max_mcap = 0.0
        self.last_q = float(qip) if qip else None
        self.last_ts = self.created_ts

        # outputs
        self.features_open = True
        self.snapshots: dict[int, dict] = {}
        self.entries: dict[int, list] = {}
        self.pending_entries: list[int] = []
        self.path: list[list] = []
        self._tail: Optional[list] = None
        if price0:
            self.path.append([self.created_ts, price0, self.last_q])

    # ------------------------------------------------------------------ trades
    def on_trade(self, ev: dict, path_step: float):
        ts = ev.get("timestamp") or self.last_ts
        price = ev.get("price")
        if not price:
            ta, qa = ev.get("tokenAmount"), ev.get("quoteAmount")
            price = (float(qa) / float(ta)) if ta and qa else None
        q = ev.get("quoteInPool")
        q = float(q) if q is not None else self.last_q
        if ev.get("pool"):
            self.pools.add(ev["pool"])

        if self.features_open:
            self._aggregate(ev, ts)

        if price:
            price = float(price)
            if self.first_price is None:
                self.first_price = price
            self.last_price = price
            self.last_q = q
            if price > self.max_price:
                self.max_price = price
            mc = ev.get("marketCapQuote")
            if mc is not None:
                self.last_mcap = float(mc)
                self.max_mcap = max(self.max_mcap, self.last_mcap)
            # entries for checkpoints whose decision time has passed
            if self.pending_entries:
                for cp in self.pending_entries:
                    self.entries[cp] = [ts, price, q]
                self.pending_entries = []
            self._path_point(ts, price, q, path_step)
        self.last_ts = ts

    def _aggregate(self, ev: dict, ts: int):
        if ev.get("txSigner"):
            self.signers.add(ev["txSigner"])
        legs = ev.get("breakdown") or [{
            "trader": (ev.get("tradersInvolved") or [ev.get("txSigner")])[0],
            "action": ev.get("action"),
            "tokenAmount": ev.get("tokenAmount"),
            "quoteAmount": ev.get("quoteAmount"),
        }]
        same_block = ev.get("block") is not None and ev.get("block") == self.created_block
        for leg in legs:
            trader = leg.get("trader")
            qa = float(leg.get("quoteAmount") or 0.0)
            ta = float(leg.get("tokenAmount") or 0.0)
            is_dev = trader is not None and trader == self.creator
            if leg.get("action") == "buy":
                self.n_buys += 1
                self.buy_vol += qa
                if is_dev:
                    self.dev_bought_tokens += ta
                elif trader:
                    self.buyers[trader] = self.buyers.get(trader, 0.0) + qa
                    if same_block:
                        self.bundle_buyers.add(trader)
                        self.bundle_vol += qa
                self.recent.append((ts, qa))
            elif leg.get("action") == "sell":
                self.n_sells += 1
                self.sell_vol += qa
                if trader:
                    self.sellers.add(trader)
                if is_dev:
                    self.dev_sold_tokens += ta
                    self.dev_sell_count += 1
                self.recent.append((ts, -qa))

    def _path_point(self, ts, price, q, step):
        pt = [ts, price, q]
        if not self.path:
            self.path.append(pt)
            self._tail = None
            return
        ref = self.path[-1][1]
        if abs(math.log(price / ref)) >= math.log1p(step):
            self.path.append(pt)
            self._tail = None
        else:
            self._tail = pt  # keep the latest price so the final value is exact

    # ------------------------------------------------------------ snapshots
    def snapshot(self, cp: int) -> dict:
        now = self.created_ts + cp
        while self.recent and self.recent[0][0] < now - 60:
            self.recent.popleft()
        flow_30 = sum(v for t, v in self.recent if t > now - 30)
        buys_30 = sum(1 for t, v in self.recent if t > now - 30 and v > 0)
        flow_60 = sum(v for _t, v in self.recent)
        top = sorted(self.buyers.values(), reverse=True)
        non_dev_buy_vol = sum(top)
        supply = float(self.supply or 1e9)
        dev_tokens = self.dev_bought_tokens or self.dev_initial_buy_tokens
        first = self.first_price or self.last_price
        return {
            "age_s": cp,
            "protocol": self.protocol,
            "launch_type": self.launch_type,
            "quote_is_sol": self.quote_mint == SOL_MINT,
            "has_authority": bool(self.mint_authority or self.freeze_authority),
            "risky_extension": any(x in RISKY_EXTENSIONS for x in self.extensions),
            "creator_prev_launches": self.creator_prev_launches,
            "creator_prev_runners": self.creator_prev_runners,
            "creator_open_launches": self.creator_open_launches,
            "dev_initial_buy": self.dev_initial_buy_quote,
            "dev_initial_pct": 100.0 * dev_tokens / supply if supply else 0.0,
            "dev_sold": self.dev_sell_count > 0,
            "dev_sold_pct": (100.0 * self.dev_sold_tokens / dev_tokens) if dev_tokens else 0.0,
            "n_buys": self.n_buys,
            "n_sells": self.n_sells,
            "buy_sell_ratio": self.n_buys / max(1, self.n_sells),
            "unique_buyers": len(self.buyers),
            "unique_sellers": len(self.sellers),
            "unique_signers": len(self.signers),
            "buy_vol": round(self.buy_vol, 6),
            "sell_vol": round(self.sell_vol, 6),
            "net_flow": round(self.buy_vol - self.sell_vol, 6),
            "flow_30s": round(flow_30, 6),
            "flow_60s": round(flow_60, 6),
            "buys_30s": buys_30,
            "avg_buy": self.buy_vol / self.n_buys if self.n_buys else 0.0,
            "top1_share": (top[0] / non_dev_buy_vol) if top and non_dev_buy_vol else 0.0,
            "top3_share": (sum(top[:3]) / non_dev_buy_vol) if top and non_dev_buy_vol else 0.0,
            "bundle_buyers": len(self.bundle_buyers),
            "bundle_share": (self.bundle_vol / non_dev_buy_vol) if non_dev_buy_vol else 0.0,
            "mcap": self.last_mcap or 0.0,
            "max_mcap": self.max_mcap,
            "quote_in_pool": self.last_q or 0.0,
            "mult_from_launch": (self.last_price / first) if first and self.last_price else 1.0,
            "drawdown_from_max": (self.last_price / self.max_price) if self.max_price and self.last_price else 1.0,
            "migrated": self.migrated,
            "curve_complete": self.curve_complete,
            "liq_removes": self.liq_removes,
            "fee_claims": self.fee_claims,
        }

    def peak_mult(self) -> float:
        return (self.max_price / self.first_price) if self.first_price and self.max_price else 1.0

    def to_record(self, horizon_s: int, complete: bool) -> dict:
        path = list(self.path)
        if self._tail is not None:
            path.append(self._tail)
        return {
            "mint": self.mint,
            "protocol": self.protocol,
            "launch_type": self.launch_type,
            "created_ts": self.created_ts,
            "creator": self.creator,
            "name": self.name,
            "symbol": self.symbol,
            "quote_mint": self.quote_mint,
            "horizon_s": horizon_s,
            "complete": complete,
            "migrated": self.migrated,
            "migrate_ts": self.migrate_ts,
            "peak_mult_launch": round(self.peak_mult(), 4),
            "snapshots": {str(k): v for k, v in self.snapshots.items()},
            "entries": {str(k): v for k, v in self.entries.items()},
            "path": path,
        }


class ReplayEngine:
    """Feed events in time order with `process`; finished tokens go to `on_record`.

    Callbacks:
        on_record(record)            token finished its horizon (dataset row)
        on_snapshot(state, cp, feat) a checkpoint was reached (live decisions)
    """

    def __init__(self, config: EngineConfig = EngineConfig(),
                 on_record: Optional[Callable[[dict], None]] = None,
                 on_snapshot: Optional[Callable[[TokenState, int, dict], None]] = None,
                 on_trade: Optional[Callable[[TokenState, dict], None]] = None):
        self.cfg = config
        self.checkpoints = tuple(sorted(int(c) for c in config.checkpoints))
        self.on_record = on_record
        self.on_snapshot = on_snapshot
        self.on_trade = on_trade
        self.tokens: dict[str, TokenState] = {}
        self.pool_to_mint: dict[str, str] = {}
        self.seen_mints: set = set()
        self.creator_stats: dict[str, list] = {}  # creator -> [launches, runners] (finalised only)
        self.creator_open: dict[str, int] = {}
        self._cp_heap: list = []
        self._end_heap: list = []
        self._seq = 0
        self.clock = 0
        self._sig = None
        self._sig_trades: list = []
        self.n_events = 0
        self.n_records = 0

    # --------------------------------------------------------------- driving
    def run(self, events: Iterable[dict], flush: bool = True):
        for ev in events:
            self.process(ev)
        if flush:
            self.flush()

    def process(self, ev: dict):
        self.n_events += 1
        ts = ev.get("timestamp")
        if ts is not None and ts > 1e11:  # archive in milliseconds
            ts = ev["timestamp"] = int(ts // 1000)
        if ts is not None and ts > self.clock:
            self.advance(ts)
        sig = ev.get("signature")
        if sig != self._sig:
            self._sig = sig
            self._sig_trades = []
        action = ev.get("action")
        if action in TRADE_ACTIONS:
            st = self._token_for(ev)
            if st is None:
                # trades precede creates inside a transaction; keep them in case
                # a create for this mint follows in the same signature
                self._sig_trades.append(ev)
                return
            self._apply_trade(st, ev)
        elif action == "create":
            self._launch(ev, "create")
        elif action == "createPool":
            mint = ev.get("mint")
            if mint and mint in self.tokens:
                self._register_pool(self.tokens[mint], ev.get("pool"))
            elif mint and self.cfg.include_pool_launches and mint not in self.seen_mints:
                self._launch(ev, "pool")
        elif action == "migrate":
            mint = ev.get("mint") or self.pool_to_mint.get(ev.get("fromPool"))
            st = self.tokens.get(mint) if mint else None
            if st:
                st.migrated = True
                st.migrate_ts = ts
                self._register_pool(st, ev.get("toPool"))
        elif action == "curveComplete":
            st = self._token_for(ev)
            if st:
                st.curve_complete = True
        elif action == "remove":
            st = self._token_for(ev)
            if st and st.features_open:
                st.liq_removes += 1
        elif action == "claimCreatorFees":
            st = self._token_for(ev)
            if st and st.features_open:
                st.fee_claims += 1

    def advance(self, ts: int):
        """Move the clock: fire checkpoints and finalise tokens strictly before ts."""
        self.clock = ts
        cp_heap, end_heap = self._cp_heap, self._end_heap
        while cp_heap and cp_heap[0][0] < ts:
            _due, _s, mint, cp = heapq.heappop(cp_heap)
            st = self.tokens.get(mint)
            if st is None:
                continue
            feat = st.snapshot(cp)
            st.snapshots[cp] = feat
            st.pending_entries.append(cp)
            if cp == self.checkpoints[-1]:
                st.features_open = False
                st.buyers = {}
                st.sellers = set()
                st.signers = set()
                st.recent.clear()
            if self.on_snapshot:
                self.on_snapshot(st, cp, feat)
        while end_heap and end_heap[0][0] < ts:
            _due, _s, mint = heapq.heappop(end_heap)
            self._finalise(mint, complete=True)

    def flush(self):
        """Fire remaining checkpoints that are due and emit unfinished tokens as incomplete."""
        for mint in list(self.tokens):
            self._finalise(mint, complete=False)

    # --------------------------------------------------------------- helpers
    def _token_for(self, ev: dict) -> Optional[TokenState]:
        mint = ev.get("mint")
        if mint and mint in self.tokens:
            return self.tokens[mint]
        pool = ev.get("pool")
        if pool and pool in self.pool_to_mint:
            return self.tokens.get(self.pool_to_mint[pool])
        return None

    def _apply_trade(self, st: TokenState, ev: dict):
        qm = ev.get("quoteMint")
        if qm and qm != st.quote_mint:
            return  # a different quote asset would break price continuity
        st.on_trade(ev, self.cfg.path_step)
        if self.on_trade:
            self.on_trade(st, ev)

    def _register_pool(self, st: TokenState, pool: Optional[str]):
        if pool:
            st.pools.add(pool)
            self.pool_to_mint[pool] = st.mint

    def _launch(self, ev: dict, launch_type: str):
        mint = ev.get("mint")
        if not mint or mint in self.seen_mints:
            return
        self.seen_mints.add(mint)
        if self.cfg.protocols and ev.get("protocol") not in self.cfg.protocols:
            return
        if self.cfg.sol_only and (ev.get("quoteMint") or SOL_MINT) != SOL_MINT:
            return
        creator = ev.get("creator") or ev.get("txSigner")
        hist = tuple(self.creator_stats.get(creator, (0, 0))) if creator else (0, 0)
        st = TokenState(mint, ev, launch_type, hist, self.creator_open.get(creator, 0) if creator else 0)
        if creator:
            self.creator_open[creator] = self.creator_open.get(creator, 0) + 1
        if not st.created_ts:
            st.created_ts = self.clock
        self.tokens[mint] = st
        self._register_pool(st, ev.get("pool"))
        for cp in self.checkpoints:
            self._seq += 1
            heapq.heappush(self._cp_heap, (st.created_ts + cp, self._seq, mint, cp))
        self._seq += 1
        heapq.heappush(self._end_heap, (st.created_ts + self.cfg.horizon_s, self._seq, mint))
        # replay trades from the same transaction that arrived before the create
        for tev in self._sig_trades:
            if tev.get("mint") == mint or tev.get("pool") in st.pools:
                self._apply_trade(st, tev)

    def _finalise(self, mint: str, complete: bool):
        st = self.tokens.pop(mint, None)
        if st is None:
            return
        for p in st.pools:
            if self.pool_to_mint.get(p) == mint:
                del self.pool_to_mint[p]
        if st.creator in self.creator_open:
            self.creator_open[st.creator] -= 1
            if self.creator_open[st.creator] <= 0:
                del self.creator_open[st.creator]
        if complete and st.creator:
            cs = self.creator_stats.setdefault(st.creator, [0, 0])
            cs[0] += 1
            if st.peak_mult() >= RUNNER_MULT:
                cs[1] += 1
        self.n_records += 1
        if self.on_record:
            self.on_record(st.to_record(self.cfg.horizon_s, complete))
