import math

import pytest

from runnerbt import SOL_MINT
from runnerbt.engine import EngineConfig, ReplayEngine
from runnerbt.live import LiveDecider
from runnerbt.simulate import label, run_backtest, simulate_trade
from runnerbt.strategy import Strategy
from runnerbt.synth import generate


def create(ts, mint="M", creator="DEV", block=1, sig="c1", **kw):
    ev = {"signature": sig, "block": block, "timestamp": ts, "action": "create", "protocol": "PUMPFUN",
          "txSigner": creator, "mint": mint, "pool": "P" + mint, "quoteMint": SOL_MINT, "creator": creator,
          "supply": 1_000_000_000, "tokensInPool": 1_000_000_000, "quoteInPool": 30.0,
          "mintAuthority": None, "freezeAuthority": None, "tokenExtensions": []}
    ev.update(kw)
    return ev


def trade(ts, side, price, trader="T", quote=1.0, mint="M", block=5, sig=None):
    return {"signature": sig or f"s{ts}{trader}{side}{price}", "block": block, "timestamp": ts, "action": side,
            "protocol": "PUMPFUN", "txSigner": trader, "pool": "P" + mint, "mint": mint, "quoteMint": SOL_MINT,
            "tokenAmount": quote / price, "quoteAmount": quote, "price": price, "marketCapQuote": price * 1e9,
            "quoteInPool": 30.0, "tradersInvolved": [trader],
            "breakdown": [{"trader": trader, "action": side, "tokenAmount": quote / price,
                           "quoteAmount": quote, "price": price}]}


def run(events, **cfg):
    recs = []
    eng = ReplayEngine(EngineConfig(**cfg), on_record=recs.append)
    eng.run(events)
    return recs


def test_snapshot_is_point_in_time():
    p = 3e-8
    evs = [create(100), trade(105, "buy", p, "A"), trade(130, "buy", p * 2, "B"),
           trade(131, "buy", p * 9, "C"), trade(500, "buy", p * 9, "D")]
    (r,) = run(evs, checkpoints=(30,), horizon_s=1000)
    snap = r["snapshots"]["30"]
    assert snap["n_buys"] == 2          # trade at ts=130 (== created+30) is included, 131 is not
    assert snap["unique_buyers"] == 2
    entry = r["entries"]["30"]
    assert entry[0] == 131 and entry[1] == pytest.approx(p * 9)  # enter on the next trade, never before


def test_trade_before_create_in_same_tx_is_kept():
    evs = [trade(100, "buy", 3e-8, "DEV", quote=2.0, sig="c1", block=1), create(100, sig="c1"),
           trade(110, "sell", 3e-8, "DEV", quote=1.0), trade(200, "buy", 3e-8, "X")]
    (r,) = run(evs, checkpoints=(30,), horizon_s=1000)
    s = r["snapshots"]["30"]
    assert s["n_buys"] == 1 and s["dev_sold"] is True
    assert s["unique_buyers"] == 0       # the dev is not counted as an outside buyer


def test_horizon_and_incomplete():
    evs = [create(100), trade(120, "buy", 1e-7), create(150, mint="N"), trade(5000, "buy", 1e-7, mint="N")]
    recs = run(evs, checkpoints=(10,), horizon_s=1000)
    by = {r["mint"]: r for r in recs}
    assert by["M"]["complete"] is True
    assert by["N"]["complete"] is True  # ts 5000 > 150 + 1000 finalises N before that trade is applied
    assert by["N"]["path"][-1][1] != 1e-7


def test_runner_label_and_take_profit():
    p = 1e-8
    evs = [create(0), trade(40, "buy", p)]
    evs += [trade(60 + i * 10, "buy", p * m) for i, m in enumerate([1.5, 2, 3, 4, 5.5, 8, 12, 4])]
    (r,) = run(evs, checkpoints=(30,), horizon_s=3600)
    lab = label(r, 30)
    assert lab["runner"] and lab["max_mult"] == pytest.approx(12)
    s = Strategy.from_dict({"checkpoints": [30], "exit": {
        "take_profit": [{"mult": 5, "frac": 0.5}, {"mult": 10, "frac": 1.0}], "stop_loss": 0.5, "trailing": None},
        "position": 1.0, "fee_pct": 0.0, "slippage_pct": 0.0})
    t = simulate_trade(r, 30, s)
    # no costs: half sold at 5x and half at 10x (both minus constant-product impact)
    assert t["exit_reason"] == "tp10"
    assert 5.0 < t["proceeds"] < 7.5


def test_stop_loss_exits_at_gapped_price():
    p = 1e-8
    evs = [create(0), trade(40, "buy", p), trade(50, "sell", p * 0.05)]
    (r,) = run(evs, checkpoints=(30,), horizon_s=3600)
    s = Strategy.from_dict({"checkpoints": [30], "exit": {"take_profit": [{"mult": 5, "frac": 1}],
                            "stop_loss": 0.5, "trailing": None},
                            "position": 1.0, "fee_pct": 0.0, "slippage_pct": 0.0})
    t = simulate_trade(r, 30, s)
    assert t["exit_reason"] == "stop"
    assert t["proceeds"] < 0.06       # a rug gaps through the stop; no fill at 0.5x


def test_filters():
    s = Strategy.from_dict({"filters": {"n_buys": {"min": 5}, "dev_sold": False,
                                        "protocol": {"in": ["PUMPFUN"]}}})
    assert s.decide({"n_buys": 6, "dev_sold": False, "protocol": "PUMPFUN"})["enter"]
    assert not s.decide({"n_buys": 4, "dev_sold": False, "protocol": "PUMPFUN"})["enter"]
    assert not s.decide({"n_buys": 9, "dev_sold": True, "protocol": "PUMPFUN"})["enter"]
    assert not s.decide({"n_buys": 9, "dev_sold": False, "protocol": "BONK"})["enter"]


def test_live_decisions_match_backtest_entries():
    events = generate(n_tokens=150, hours=4, seed=3)
    strat = Strategy.from_dict({"checkpoints": [30, 60], "filters": {"unique_buyers": {"min": 5}}})
    cfg = EngineConfig(checkpoints=(30, 60), horizon_s=3600)

    recs = run(events, checkpoints=(30, 60), horizon_s=3600)
    bt = run_backtest([dict(r, complete=True) for r in recs], strat)
    bt_entries = {(t["mint"], t["checkpoint"]) for t in bt["trade_list"]}

    live = []
    dec = LiveDecider(strat, live.append, engine_cfg=cfg)
    for ev in events:
        dec.process(ev)
    dec.engine.flush()
    live_entries = {(d["mint"], d["checkpoint"]) for d in live if d["enter"]}
    # every backtest entry was flagged live at the same checkpoint (live may also flag tokens
    # that never traded again, which the backtest cannot fill)
    assert bt_entries and bt_entries <= live_entries


def test_model_roundtrip(tmp_path):
    pytest.importorskip("numpy")
    from runnerbt.model import LogisticModel, build_xy
    recs = run(generate(n_tokens=300, hours=6, seed=5), checkpoints=(30,), horizon_s=3 * 3600)
    X, y, _ = build_xy([r for r in recs if r["complete"]], 30)
    m = LogisticModel.fit(X, y, checkpoint=30)
    path = tmp_path / "m.json"
    m.save(str(path))
    m2 = LogisticModel.load(str(path))
    feat = next(r for r in recs if "30" in r["snapshots"])["snapshots"]["30"]
    assert math.isclose(m.predict_one(feat), m2.predict_one(feat))
    assert 0.0 <= m2.predict_one(feat) <= 1.0


def test_stream_unpack_handles_batches():
    from runnerbt.stream import unpack
    e = {"action": "buy", "signature": "a"}
    assert unpack(e) == [e]
    assert unpack([e, e]) == [e, e]
    assert unpack([[e], e]) == [e, e]
    assert unpack({"events": [e]}) == [e]
    assert unpack('[{"action": "buy", "signature": "a"}]') == [e]
    assert unpack(None) == []


def test_fetch_template_and_sniff():
    import gzip
    import json
    from runnerbt.fetch import hours_between, parse_when, render, sniff_ext
    t = parse_when("2026-09-30T07")
    assert render("https://x/{date}/{HH}.jsonl.gz?h={hour}&u={unix}", t) == \
        f"https://x/2026-09-30/07.jsonl.gz?h=7&u={int(t.timestamp())}"
    assert len(list(hours_between(parse_when("2026-09-30"), parse_when("2026-10-01")))) == 24
    line = json.dumps({"action": "buy", "signature": "s"}).encode()
    assert sniff_ext(gzip.compress(line + b"\n" + line)) == ".jsonl.gz"
    assert sniff_ext(b"[" + line + b"]") == ".json"


def test_millisecond_timestamps_are_normalised():
    evs = [create(1_790_000_000), trade(1_790_000_005, "buy", 3e-8), trade(1_790_000_050, "buy", 3e-8, "B")]
    for e in evs:
        e["timestamp"] *= 1000
    (r,) = run(evs, checkpoints=(10,), horizon_s=1000)
    assert r["created_ts"] == 1_790_000_000 and r["snapshots"]["10"]["n_buys"] == 1
