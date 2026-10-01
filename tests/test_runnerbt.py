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
    assert by["N"]["path"] == []  # its only trade came after the horizon: never applied


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


def test_fetch_hours_and_urls():
    from runnerbt.fetch import DEFAULT_TEMPLATE, hour_key, hours_between, parse_when, render
    t = parse_when("2026-09-14T12")
    assert render(DEFAULT_TEMPLATE, t) == "https://replay.shrine.trade/pump/2026/09/14/12.jsonl.zst"
    assert parse_when("2026/09/14/12") == t and hour_key(t) == "2026/09/14/12"
    assert len(list(hours_between(parse_when("2026-09-30"), parse_when("2026-10-01")))) == 24


def test_millisecond_timestamps_are_normalised():
    evs = [create(1_790_000_000), trade(1_790_000_005, "buy", 3e-8), trade(1_790_000_050, "buy", 3e-8, "B")]
    for e in evs:
        e["timestamp"] *= 1000
    (r,) = run(evs, checkpoints=(10,), horizon_s=1000)
    assert r["created_ts"] == 1_790_000_000 and r["snapshots"]["10"]["n_buys"] == 1


def test_slim_keeps_everything_the_backtest_needs():
    from runnerbt.fetch import Slimmer
    events = generate(n_tokens=120, hours=4, seed=21)
    noise = [{"signature": f"n{i}", "block": 1, "timestamp": e["timestamp"], "action": "buy",
              "protocol": "PUMPSWAP", "mint": "OLDMINT", "pool": "OLDPOOL", "price": 1.0}
             for i, e in enumerate(events[::3])]
    full = sorted(events + noise, key=lambda e: e["timestamp"])
    sl = Slimmer(keep_hours=24)
    slim = [k for e in full for k in sl.feed(e)]
    assert not any(e.get("mint") == "OLDMINT" for e in slim)
    a = {r["mint"]: r for r in run(full, checkpoints=(30, 60), horizon_s=3600)}
    b = {r["mint"]: r for r in run(slim, checkpoints=(30, 60), horizon_s=3600)}
    assert a.keys() == b.keys()
    for m in a:
        assert a[m]["snapshots"] == b[m]["snapshots"] and a[m]["path"] == b[m]["path"]


def test_zst_files_are_readable(tmp_path):
    zstandard = pytest.importorskip("zstandard")
    from runnerbt.io import iter_events
    d = tmp_path / "2026" / "09" / "14"
    d.mkdir(parents=True)
    (d / "12.jsonl.zst").write_bytes(zstandard.ZstdCompressor().compress(b'{"action":"buy","signature":"a"}\n'))
    (tmp_path / "_slim_state.json").write_text("{}")
    assert [e["signature"] for e in iter_events([str(tmp_path)])] == ["a"]


def test_hunt_finds_planted_signal_and_ignores_protocol():
    pytest.importorskip("numpy")
    from runnerbt.hunt import hunt, wilson_lower
    from runnerbt.model import feature_names

    assert not any(n.startswith(("protocol", "launch_type")) for n in feature_names())
    assert wilson_lower(30, 100) < 0.3 < wilson_lower(60, 100)

    # 1000 launches on two protocols; runners are exactly those with >= 40 unique buyers, on both
    recs = []
    for i in range(1000):
        strong = i % 7 == 0
        top = 10.0 if strong else 1.5
        recs.append({
            "mint": f"m{i}", "protocol": "PUMPFUN" if i % 2 else "BONK", "created_ts": i * 100,
            "horizon_s": 3600, "complete": True,
            "snapshots": {"30": {"unique_buyers": 40 + i % 9 if strong else i % 39, "n_buys": 50,
                                 "protocol": "PUMPFUN" if i % 2 else "BONK"}},
            "entries": {"30": [i * 100 + 31, 1.0, 1e9]},
            "path": [[i * 100 + 40, top, 1e9]],
        })
    train, test = recs[:700], recs[700:]
    results, _ = hunt(train, test, [30], targets=(0.5,), slippage_pct=0.0, use_model=False, log=lambda *_: None)
    (r,) = results
    assert any(c.feature == "unique_buyers" for c in r.conds)
    assert r.test_hits == r.test_runners and r.test_precision >= 0.5  # every runner, above target


def test_live_after_warmup_only_decides_new_launches(tmp_path):
    import time as _time
    from runnerbt.io import write_jsonl
    old = generate(n_tokens=60, hours=1, seed=8, start_ts=int(_time.time()) - 5 * 3600)
    # a token launched in the archive's last seconds: its 10s check is still pending when warm-up ends
    tail_ts = max(e["timestamp"] for e in old) + 1
    old += [create(tail_ts, mint="TAIL", sig="tail"), trade(tail_ts + 2, "buy", 3e-8, "X", mint="TAIL")]
    write_jsonl(str(tmp_path / "old.jsonl.gz"), old)
    strat = Strategy.from_dict({"checkpoints": [10], "filters": {}})  # buy everything
    got = []
    dec = LiveDecider(strat, got.append)
    dec.warmup([str(tmp_path)])
    assert got == []
    assert dec.engine.clock >= int(_time.time()) - 10  # caught up before going live: no big jump later
    now = int(_time.time())
    live = generate(n_tokens=40, hours=0.02, seed=9, start_ts=now - 30)
    for ev in live:
        dec.process(ev)
    old_mints = {e["mint"] for e in old if e.get("action") == "create"}
    assert got, "new launches should be decided"
    for d in got:
        assert d["mint"] not in old_mints
        assert d["created_ts"] >= dec.live_start
    assert "TAIL" not in {d["mint"] for d in got}  # its pending check fired silently during warm-up


def test_tick_waits_for_stream_lag():
    strat = Strategy.from_dict({"checkpoints": [10], "filters": {}})
    got = []
    dec = LiveDecider(strat, got.append)
    t0 = 1_790_000_000
    dec.process(create(t0, block=1))
    for _ in range(50):
        dec._lags.append(3.0)  # the stream runs 3s behind the wall clock
    dec.tick(now=t0 + 12.5)    # wall clock: 12.5s after launch, stream has only reached ~9.5s
    assert got == []
    dec.process(trade(t0 + 9, "buy", 3e-8, "LATE"))  # a trade from before the checkpoint arrives late
    dec.tick(now=t0 + 15)
    assert len(got) == 1 and got[0]["features"]["n_buys"] == 1


def test_price_units_and_quote_sanity():
    # reported price 1e6 too small (wrong units) and marketCapQuote ~0: we fix both
    p = 3e-8
    bad = [trade(110 + i, "buy", p, f"T{i}") for i in range(5)] + [trade(200, "buy", p, "Z")]
    for e in bad:
        e["price"] = p / 1e6
        e["marketCapQuote"] = 0.0
        for leg in e["breakdown"]:
            leg["price"] = p / 1e6
    (r,) = run([create(100)] + bad, checkpoints=(30,), horizon_s=1000)
    snap = r["snapshots"]["30"]
    assert snap["mcap"] == pytest.approx(30.0, rel=0.05)   # 3e-8 SOL x 1e9 supply
    assert r["entries"]["30"][1] == pytest.approx(p, rel=0.05)

    # launches paired against another token are skipped by default ...
    other = "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W"
    evs = [create(100, quoteMint=other), trade(105, "buy", p, mint="M"), trade(200, "buy", p, "Q")]
    evs[1]["quoteMint"] = evs[2]["quoteMint"] = other
    assert run(evs, checkpoints=(30,), horizon_s=1000) == []
    # ... also when only the trades reveal the quote
    evs[0].pop("quoteMint")
    assert run(evs, checkpoints=(30,), horizon_s=1000) == []

    # a "launch" that starts at thousands of SOL is mislabeled and dropped
    big = [create(100), trade(105, "buy", 5e-5), trade(200, "buy", 5e-5, "Q")]  # 50,000 SOL mcap
    assert run(big, checkpoints=(30,), horizon_s=1000) == []


def test_bundle_features_catch_bundles_after_the_launch_block():
    p = 3e-8
    evs = [create(100, block=1000)]
    # 4 wallets in the block right after launch, each buying 2% of supply (20M tokens)
    for i in range(4):
        evs.append(trade(101, "buy", p, f"B{i}", quote=20_000_000 * p, block=1001))
    # one bundler dumps everything, plus a normal buyer later
    evs.append(trade(110, "sell", p, "B0", quote=20_000_000 * p, block=1025))
    evs.append(trade(115, "buy", p, "N", quote=1_000_000 * p, block=1040))
    evs.append(trade(200, "buy", p, "Z", block=1300))
    (r,) = run(evs, checkpoints=(30,), horizon_s=1000)
    f = r["snapshots"]["30"]
    assert f["launch_block_pct"] == 0                      # nothing in the launch block itself...
    assert f["early_slots_pct"] == pytest.approx(8.0)      # ...but 8% within the first slots
    assert f["bundle_slot_pct"] == pytest.approx(8.0)
    assert f["max_slot_buyers"] == 4
    assert f["top10_hold_pct"] == pytest.approx(6.1)       # 3 x 2% still held + 0.1%
    assert f["holders"] == 4


def test_sells_of_tokens_never_bought_are_flagged():
    p = 3e-8
    evs = [create(100, block=1000), trade(101, "buy", p, "A", quote=10_000_000 * p, block=1001),
           # "X" never bought on the curve but sells 5% of supply (allocation or transfer)
           trade(110, "sell", p, "X", quote=50_000_000 * p, block=1020),
           trade(200, "buy", p, "Z", block=1300)]
    (r,) = run(evs, checkpoints=(30,), horizon_s=1000)
    f = r["snapshots"]["30"]
    assert f["unbought_sell_pct"] == pytest.approx(5.0)
    assert f["unbought_sellers"] == 1
    assert f["launch_mcap"] == pytest.approx(30.0, rel=0.05)
