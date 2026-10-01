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

STREAM_URL = "https://sol.shrine.trade"


class LiveDecider:
    def __init__(self, strategy: Strategy, on_decision: Callable[[dict], None],
                 horizon_s: int = 3600, report_rejects: bool = False, engine_cfg: Optional[EngineConfig] = None):
        self.strategy = strategy
        self.on_decision = on_decision
        self.report_rejects = report_rejects
        self.decided: set = set()
        cfg = engine_cfg or EngineConfig(checkpoints=tuple(strategy.checkpoints), horizon_s=horizon_s)
        self.engine = ReplayEngine(cfg, on_snapshot=self._on_snapshot,
                                   on_record=lambda r: self.decided.discard(r["mint"]))

    def process(self, ev: dict):
        self.engine.process(ev)

    def tick(self, now: Optional[int] = None):
        """Fire due checkpoints even when the market is quiet (call every second or so)."""
        self.engine.advance(int(now or time.time()))

    def _on_snapshot(self, st: TokenState, cp: int, feat: dict):
        if st.mint in self.decided or cp not in self.strategy.checkpoints:
            return
        d = self.strategy.decide(feat)
        last_cp = cp == self.strategy.checkpoints[-1]
        if d["enter"] or last_cp:
            self.decided.add(st.mint)
        if d["enter"] or self.report_rejects:
            self.on_decision({
                "mint": st.mint, "symbol": st.symbol, "name": st.name, "protocol": st.protocol,
                "checkpoint": cp, "price": st.last_price, "enter": d["enter"],
                "reason": d["reason"], "score": d["score"], "features": feat,
                "strategy": self.strategy.name, "exit_plan": self.strategy.exit,
            })


def run_socket(decider: LiveDecider, protocols=None, actions=None, url: str = STREAM_URL):
    """Connect to the Advanced Data Stream and drive a LiveDecider (blocking)."""
    import socketio  # python-socketio[client]

    sio = socketio.Client(reconnection=True)

    @sio.event
    def connect():
        payload = {}
        if protocols:
            payload["protocols"] = list(protocols)
        if actions:
            payload["actions"] = list(actions)
        sio.emit("subscribe_stream", payload)
        print(f"connected to {url}, subscribed {payload or 'all'}", file=sys.stderr)

    @sio.on("stream")
    def on_stream(ev):
        decider.process(ev)

    sio.connect(url, transports=["websocket"])
    try:
        while True:
            sio.sleep(1)
            decider.tick()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sio.emit("unsubscribe_stream")
        finally:
            sio.disconnect()


def print_decision(d: dict):
    out = {k: d[k] for k in ("mint", "symbol", "protocol", "checkpoint", "enter", "reason", "score", "price")}
    print(json.dumps(out), flush=True)
