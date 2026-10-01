"""Command line: python -m runnerbt <command> ..."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

from . import RUNNER_MULT


def _parse_duration(s: str) -> int:
    s = str(s).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)


def _csv_list(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def cmd_record(a):
    from .recorder import record
    record(a.out, _csv_list(a.protocols), _csv_list(a.actions), a.url, debug=a.debug)


def cmd_fetch(a):
    from datetime import timedelta
    from .fetch import DEFAULT_TEMPLATE, fetch, parse_when
    start = parse_when(a.start) if a.start else None
    end = parse_when(a.end) if a.end else (start + timedelta(seconds=_parse_duration(a.span))
                                            if start and a.span else None)
    if not (start or a.last or a.all):
        sys.exit("say which hours: --from 2026-09-20 [--to 2026-09-27 | --span 24h], --last 24, or --all")
    headers = {k.strip(): v.strip() for k, v in (h.split(":", 1) for h in a.header or [])}
    out = a.out or ("data/slim" if a.slim else "data/raw")
    c = fetch(out, start, end, a.last, slim=a.slim, keep_hours=a.keep_hours,
              protocols=set(_csv_list(a.protocols)) if a.protocols else None, keep_raw=a.keep_raw,
              workers=a.workers, template=a.url_template or DEFAULT_TEMPLATE, headers=headers)
    if c.get("error"):
        sys.exit(1)


def cmd_synth(a):
    from .io import write_jsonl
    from .synth import generate
    events = generate(a.tokens, a.hours, a.seed)
    path = os.path.join(a.out, "synthetic.jsonl.gz")
    n = write_jsonl(path, events)
    print(f"wrote {n:,} synthetic events for {a.tokens} launches to {path}")


def cmd_build(a):
    from .dataset import build_dataset
    from .engine import EngineConfig
    cfg = EngineConfig(
        checkpoints=tuple(_parse_duration(x) for x in a.checkpoints.split(",")),
        horizon_s=_parse_duration(a.horizon),
        path_step=a.path_step,
        include_pool_launches=not a.no_pool_launches,
        sol_only=not a.all_quotes,
        protocols=set(_csv_list(a.protocols)) if a.protocols else None,
    )
    st = build_dataset(a.input, a.out, cfg, include_incomplete=a.include_incomplete)
    print(json.dumps(st))


def cmd_stats(a):
    from .dataset import load_records
    from .simulate import label
    recs = load_records(a.dataset)
    cps = sorted({int(k) for r in recs for k in r.get("snapshots", {})})
    print(f"{len(recs):,} complete launches")
    by_proto: dict = {}
    for r in recs:
        b = by_proto.setdefault(r["protocol"], [0, 0, 0])
        b[0] += 1
        b[1] += r["peak_mult_launch"] >= RUNNER_MULT
        b[2] += bool(r.get("migrated"))
    print(f"\n{'protocol':<16}{'launches':>10}{'5x from launch':>16}{'migrated':>10}")
    for p, (n, run, mig) in sorted(by_proto.items(), key=lambda kv: -kv[1][0]):
        print(f"{str(p):<16}{n:>10}{run:>10} ({run / n:5.1%}){mig:>10}")
    print(f"\n{'entry at':<10}{'tradable':>10}{'5x+ after entry':>18}{'10x+':>8}{'median t->5x':>14}")
    for cp in cps:
        labs = [label(r, cp) for r in recs]
        labs = [x for x in labs if x]
        r5 = [x for x in labs if x["max_mult"] >= 5]
        r10 = [x for x in labs if x["max_mult"] >= 10]
        tt = sorted(x["time_to_target"] for x in r5)
        med = f"{tt[len(tt) // 2] / 60:.1f}m" if tt else "-"
        print(f"{cp:>6}s   {len(labs):>10}{len(r5):>10} ({len(r5) / max(1, len(labs)):5.1%}){len(r10):>8}{med:>14}")


def _write_trades(path, trades):
    if not trades:
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(trades[0].keys()))
        w.writeheader()
        w.writerows(trades)
    print(f"trades written to {path}")


def cmd_backtest(a):
    from .dataset import load_records, time_split
    from .simulate import format_report, run_backtest
    from .strategy import Strategy
    s = Strategy.load(a.strategy)
    if a.model:
        s.model = a.model
    if a.min_score is not None:
        s.min_score = a.min_score
    recs = load_records(a.dataset)
    if a.split:
        train, test = time_split(recs, a.split)
        for name, part in (("in-sample", train), ("out-of-sample", test)):
            print(format_report(run_backtest(part, s, keep_trades=False), f"{s.name} [{name}]"), "\n")
    m = run_backtest(recs, s)
    print(format_report(m, s.name))
    if a.trades:
        _write_trades(a.trades, m["trade_list"])
    if a.json:
        print(json.dumps({k: v for k, v in m.items() if k != "trade_list"}, indent=2, default=str))


def cmd_optimize(a):
    from .dataset import load_records, time_split
    from .optimize import optimize
    from .simulate import format_report
    from .strategy import Strategy
    base = Strategy.load(a.base)
    recs = load_records(a.dataset)
    train, test = time_split(recs, a.split)
    print(f"optimising on {len(train):,} launches, validating on {len(test):,}")
    res = optimize(train, test, base, iters=a.iters, min_trades=a.min_trades,
                   max_filters=a.max_filters, search_exits=not a.no_exits, seed=a.seed, top=a.top)
    for i, r in enumerate(res, 1):
        tr, te = r["train"], r["test"]
        print(f"\n#{i}  cp={r['strategy'].checkpoints} filters={json.dumps(r['strategy'].filters)}")
        print(f"    exit={json.dumps(r['strategy'].exit)}")
        print(f"    train: trades {tr['trades']}, runners {tr['runners_caught']}, pnl {tr['total_pnl']:.3f}, "
              f"roi {tr['roi']:.1%}")
        if te:
            print(f"    test : trades {te['trades']}, runners {te['runners_caught']}, pnl {te['total_pnl']:.3f}, "
                  f"roi {te['roi']:.1%}")
    # pick the best candidate that also holds up out of sample
    valid = [r for r in res if r["test"] and r["test"]["trades"] > 0 and r["test"]["total_pnl"] > 0] or res
    best = valid[0]["strategy"]
    best.name = a.name or f"{base.name}-optimised"
    best.save(a.out)
    print(f"\nsaved {a.out}")
    if valid[0]["test"]:
        print(format_report(valid[0]["test"], f"{best.name} [out-of-sample]"))


# Starting points; check them against your data with `python -m runnerbt bundles <dataset>`
ANTI_BUNDLE = ["early_slots_pct<=10", "bundle_slot_pct<=10", "max_slot_buyers<=3",
               "top10_hold_pct<=35", "dev_hold_pct<=8", "unbought_sell_pct<=0.5"]


def _requirements(items) -> dict:
    from .strategy import merge_filters, parse_requirement
    out: dict = {}
    for item in items or []:
        if item == "anti-bundle":
            for x in ANTI_BUNDLE:
                k, rule = parse_requirement(x)
                out = merge_filters(out, {k: rule})
            continue
        k, rule = parse_requirement(item)
        out = merge_filters(out, {k: rule})
    return out


def cmd_hunt(a):
    from .dataset import load_records, time_split
    from .hunt import best_per_target, format_frontier, hunt
    from .strategy import Strategy, merge_filters
    recs = load_records(a.dataset)
    train, test = time_split(recs, a.split)
    cps = ([_parse_duration(x) for x in a.checkpoints.split(",")] if a.checkpoints else
           sorted({int(k) for r in recs[:2000] for k in r.get("snapshots", {})}))
    targets = sorted({float(x) for x in a.targets.split(",")} | {a.precision})
    require = _requirements(a.require)
    if require:
        sample = next((r["snapshots"] for r in recs if r.get("snapshots")), {})
        snap = next(iter(sample.values()), {})
        missing = [k for k in require if k not in snap]
        if missing:
            sys.exit(f"the dataset has no {missing}: it was built by an older version. Rebuild it:\n"
                     f"  python -m runnerbt build data/slim --out {a.dataset}")
        print(f"hard limits (never buy outside them): {require}")
    print(f"hunting {a.mult:g}x runners across all protocols: {len(train):,} launches to learn from, "
          f"{len(test):,} later launches to test on; checkpoints {cps}")
    results, base = hunt(train, test, cps, targets, mult=a.mult, position=a.position,
                         slippage_pct=a.slippage, beam=a.beam, depth=a.depth, min_hits=a.min_hits,
                         use_model=not a.no_model, require=require,
                         exclude=_csv_list(a.exclude) or ())
    if not results:
        sys.exit("no rule reached the precision targets; lower --min-hits or the targets, or add data")
    print(format_frontier(results, base))
    pick = best_per_target(results).get(a.precision)
    if pick is None:
        sys.exit(f"\nnothing reaches {a.precision:.0%} precision on the training period; try a lower --precision")
    s = Strategy(name=a.name or f"hunt-{a.mult:g}x-p{int(a.precision * 100)}", checkpoints=[pick.cp])
    if pick.kind == "model":
        model_path = os.path.splitext(a.out)[0] + ".model.json"
        pick.extra["model"].save(model_path)
        s.model, s.min_score = model_path, pick.min_score
    else:
        s.filters = pick.filters()
    s.filters = merge_filters(s.filters, require)
    s.save(a.out)
    rec = pick.test_hits / pick.test_runners if pick.test_runners else 0.0
    print(f"\nchosen for {a.precision:.0%} precision: buy at {pick.cp}s when {pick.text()}")
    print(f"  learning period: {pick.train_n} buys, {pick.train_hits} runners ({pick.train_precision:.1%})")
    print(f"  unseen period  : {pick.test_n} buys, {pick.test_hits} runners ({pick.test_precision:.1%} precision, "
          f"{rec:.1%} of all {pick.test_runners} runners)")
    print(f"saved {a.out}")


def cmd_train(a):
    from .dataset import load_records, time_split
    from .model import LogisticModel, auc, build_xy
    recs = load_records(a.dataset)
    train, test = time_split(recs, a.split)
    cp = _parse_duration(a.checkpoint)
    Xtr, ytr, _ = build_xy(train, cp, a.mult)
    Xte, yte, _ = build_xy(test, cp, a.mult)
    print(f"train {len(ytr):,} rows ({int(sum(ytr))} runners), test {len(yte):,} rows ({int(sum(yte))} runners)")
    m = LogisticModel.fit(Xtr, ytr, l2=a.l2, checkpoint=cp, mult=a.mult)
    tr_s = [m.predict_vec(x) for x in Xtr]
    te_s = [m.predict_vec(x) for x in Xte]
    print(f"AUC train {auc(tr_s, ytr):.3f}   test {auc(te_s, yte):.3f}")
    if te_s:
        ranked = sorted(zip(te_s, yte), reverse=True)
        base = sum(yte) / len(yte) if yte else 0
        for frac in (0.01, 0.05, 0.1, 0.2):
            k = max(1, int(len(ranked) * frac))
            hit = sum(y for _, y in ranked[:k]) / k
            print(f"  top {frac:>4.0%} by score: {hit:6.1%} are 5x+ (base {base:.1%}), "
                  f"threshold {ranked[k - 1][0]:.3f}")
    print("strongest features:")
    for n, w in m.top_weights():
        print(f"  {n:<24}{w:+.3f}")
    m.save(a.out)
    print(f"saved {a.out}")


def cmd_export(a):
    from .dataset import load_records
    from .simulate import label
    recs = load_records(a.dataset)
    cp = _parse_duration(a.checkpoint)
    rows = []
    for r in recs:
        f = r.get("snapshots", {}).get(str(cp))
        lab = label(r, cp)
        if f and lab:
            rows.append({"mint": r["mint"], "created_ts": r["created_ts"], **f,
                         "max_mult": lab["max_mult"], "runner": lab["runner"],
                         "time_to_5x": lab["time_to_target"]})
    _write_trades(a.out, rows)


def cmd_live(a):
    from .live import BuyLog, LiveDecider, SolPrice, run_socket
    from .strategy import Strategy
    s = Strategy.load(a.strategy)
    if a.require:
        from .strategy import merge_filters
        s.filters = merge_filters(s.filters, _requirements(a.require))
    if s.model:
        s.get_model()  # fail now, not on the first launch, if the model file is missing
    log = BuyLog(a.log, SolPrice())
    dec = LiveDecider(s, log, horizon_s=_parse_duration(a.horizon), report_rejects=a.verbose)
    print(f"strategy {s.name}: decide at {s.checkpoints}s after launch"
          f"{f', model score >= {s.min_score:.3f}' if s.model else ''}"
          f"{f', limits {s.filters}' if s.filters else ''}", file=sys.stderr)
    if a.warmup:
        print(f"warming up creator history from {a.warmup} ...", file=sys.stderr)
        n = dec.warmup(a.warmup)
        print(f"  replayed {n:,} events", file=sys.stderr)
    print(f"buys are appended to {os.path.abspath(a.log)}  (Ctrl+C to stop)", file=sys.stderr)
    run_socket(dec, _csv_list(a.protocols), ["buy", "sell", "create", "createPool", "migrate",
                                             "curveComplete", "remove", "claimCreatorFees"], a.url,
               debug=a.debug)
    print(f"{dec.buys} buys this session", file=sys.stderr)


def cmd_inspect(a):
    from .inspect import dump, find_events, quote_report
    if a.quotes:
        print(quote_report(a.input))
        return
    evs = list(find_events(a.input, a.mint, a.protocol, a.action, a.limit))
    print(dump(evs) if evs else "no matching events")


def cmd_bundles(a):
    from .bundles import bundle_report
    from .dataset import load_records
    print(bundle_report(load_records(a.dataset), _parse_duration(a.checkpoint)))


def cmd_buys(a):
    from .bundles import buys_report
    print(buys_report(a.log))


def cmd_replay(a):
    """Run the live decider over archived events (what would the app have flagged?)."""
    from .io import iter_events
    from .live import LiveDecider, print_decision
    from .strategy import Strategy
    s = Strategy.load(a.strategy)
    dec = LiveDecider(s, print_decision, report_rejects=a.verbose)
    for ev in iter_events(a.input):
        dec.process(ev)


def main(argv=None):
    p = argparse.ArgumentParser(prog="runnerbt", description="Backtest launchpad entries for 5x+ runners")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record the live stream to hourly files")
    r.add_argument("--out", default="data/raw")
    r.add_argument("--protocols")
    r.add_argument("--actions")
    r.add_argument("--url", default="https://sol.shrine.trade")
    r.add_argument("--debug", action="store_true", help="print raw Socket.IO traffic")
    r.set_defaults(fn=cmd_record)

    r = sub.add_parser("fetch", help="download the Shrine hourly historical replay archive")
    r.add_argument("--from", dest="start", help="UTC start hour, e.g. 2026-09-20 or 2026-09-20T06")
    r.add_argument("--to", dest="end", help="UTC end (exclusive)")
    r.add_argument("--span", help="length instead of --to, e.g. 24h or 7d")
    r.add_argument("--last", type=int, help="only the newest N hours in the archive")
    r.add_argument("--all", action="store_true", help="every hour the archive holds")
    r.add_argument("--slim", action="store_true",
                   help="keep only launches + their trades (much smaller)")
    r.add_argument("--keep-hours", type=float, default=24,
                   help="slim: keep trades of tokens up to this long after launch (>= build --horizon)")
    r.add_argument("--protocols", help="slim: only launches from these protocols, e.g. PUMPFUN,BONK")
    r.add_argument("--keep-raw", action="store_true", help="slim: also keep the raw .zst files")
    r.add_argument("--out", help="default data/raw, or data/slim with --slim")
    r.add_argument("--workers", type=int, default=3, help="parallel downloads")
    r.add_argument("--url-template", help="override the archive URL ({yyyy} {mm} {dd} {HH} {date} {unix})")
    r.add_argument("--header", action="append", help="extra HTTP header")
    r.set_defaults(fn=cmd_fetch)

    r = sub.add_parser("synth", help="generate a synthetic event file for dry runs")
    r.add_argument("--out", default="data/synth")
    r.add_argument("--tokens", type=int, default=1500)
    r.add_argument("--hours", type=float, default=48)
    r.add_argument("--seed", type=int, default=1)
    r.set_defaults(fn=cmd_synth)

    r = sub.add_parser("build", help="replay events into a dataset of token records")
    r.add_argument("input", nargs="+", help="event files or directories (jsonl / jsonl.gz / json)")
    r.add_argument("--out", default="data/dataset.jsonl.gz")
    r.add_argument("--checkpoints", default="10,30,60,120,300", help="decision ages, e.g. 10,30,1m,5m")
    r.add_argument("--horizon", default="6h", help="how long to follow each launch")
    r.add_argument("--path-step", type=float, default=0.01)
    r.add_argument("--protocols", help="only launches from these protocols")
    r.add_argument("--all-quotes", action="store_true",
                   help="also launches paired against tokens other than SOL (their amounts are not comparable)")
    r.add_argument("--no-pool-launches", action="store_true", help="ignore createPool launches")
    r.add_argument("--include-incomplete", action="store_true")
    r.set_defaults(fn=cmd_build)

    r = sub.add_parser("stats", help="base rates of 5x runners in a dataset")
    r.add_argument("dataset")
    r.set_defaults(fn=cmd_stats)

    r = sub.add_parser("backtest", help="simulate a strategy")
    r.add_argument("dataset")
    r.add_argument("--strategy", default="strategies/default.json")
    r.add_argument("--model")
    r.add_argument("--min-score", type=float)
    r.add_argument("--split", type=float, help="also report in/out-of-sample, e.g. 0.7")
    r.add_argument("--trades", help="write trades CSV")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_backtest)

    r = sub.add_parser("optimize", help="search filters/exits, validated out-of-sample")
    r.add_argument("dataset")
    r.add_argument("--base", default="strategies/default.json")
    r.add_argument("--out", default="strategies/optimized.json")
    r.add_argument("--name")
    r.add_argument("--iters", type=int, default=400)
    r.add_argument("--split", type=float, default=0.7)
    r.add_argument("--min-trades", type=int, default=20)
    r.add_argument("--max-filters", type=int, default=5)
    r.add_argument("--no-exits", action="store_true", help="keep the base strategy's exits")
    r.add_argument("--top", type=int, default=5)
    r.add_argument("--seed", type=int, default=7)
    r.set_defaults(fn=cmd_optimize)

    r = sub.add_parser("hunt", help="entry-only search: most runners bought at a required precision")
    r.add_argument("dataset")
    r.add_argument("--precision", type=float, default=0.3, help="required share of buys that run (0.3 = 30%%)")
    r.add_argument("--targets", default="0.1,0.2,0.3,0.4,0.5", help="precision levels to report")
    r.add_argument("--checkpoints", help="decision ages to try, default: all in the dataset")
    r.add_argument("--mult", type=float, default=RUNNER_MULT, help="what counts as a runner (5 = 5x)")
    r.add_argument("--position", type=float, default=0.5, help="buy size in SOL, for price impact")
    r.add_argument("--slippage", type=float, default=1.0, help="extra entry slippage %%")
    r.add_argument("--split", type=float, default=0.7, help="share of the period to learn from")
    r.add_argument("--min-hits", type=int, default=8, help="a rule must catch at least this many runners")
    r.add_argument("--depth", type=int, default=4, help="max conditions per rule")
    r.add_argument("--beam", type=int, default=15)
    r.add_argument("--no-model", action="store_true", help="rules only, skip the scoring model")
    r.add_argument("--exclude", help="features rules may not use, e.g. mcap,max_mcap,launch_mcap")
    r.add_argument("--require", nargs="+", metavar="RULE",
                   help="hard limits, e.g. launch_block_pct<=15 bundle_buyers<=3, or the preset "
                        "anti-bundle (= " + " ".join(ANTI_BUNDLE) + ")")
    r.add_argument("--out", default="strategies/hunt.json")
    r.add_argument("--name")
    r.set_defaults(fn=cmd_hunt)

    r = sub.add_parser("train", help="fit a P(5x) scoring model")
    r.add_argument("dataset")
    r.add_argument("--checkpoint", default="60")
    r.add_argument("--out", default="models/model.json")
    r.add_argument("--split", type=float, default=0.7)
    r.add_argument("--l2", type=float, default=1.0)
    r.add_argument("--mult", type=float, default=RUNNER_MULT)
    r.set_defaults(fn=cmd_train)

    r = sub.add_parser("export", help="feature table + labels as CSV for external analysis")
    r.add_argument("dataset")
    r.add_argument("--checkpoint", default="60")
    r.add_argument("--out", default="data/features.csv")
    r.set_defaults(fn=cmd_export)

    r = sub.add_parser("live", help="run a strategy on the live stream and log every buy (paper, no orders)")
    r.add_argument("--strategy", default="strategies/hunt.json")
    r.add_argument("--log", default="data/buys.csv", help="CSV that every fired buy is appended to")
    r.add_argument("--warmup", nargs="+", help="archive folder(s) to replay first, e.g. data/slim "
                                               "(gives creator-history features the same footing as the backtest)")
    r.add_argument("--horizon", default="6h", help="how long to follow tokens; match `build --horizon`")
    r.add_argument("--require", nargs="+", metavar="RULE",
                   help="extra hard limits on top of the strategy, e.g. anti-bundle or launch_block_pct<=10")
    r.add_argument("--protocols", help="default: all launchpads")
    r.add_argument("--url", default="https://sol.shrine.trade")
    r.add_argument("--debug", action="store_true")
    r.add_argument("-v", "--verbose", action="store_true", help="also print rejected launches")
    r.set_defaults(fn=cmd_live)

    r = sub.add_parser("inspect", help="show raw archive events, or what each launchpad reports as quote")
    r.add_argument("input", nargs="+", help="archive folder(s), e.g. data/slim")
    r.add_argument("--quotes", action="store_true", help="quote mints and launch market caps per protocol")
    r.add_argument("--mint")
    r.add_argument("--protocol")
    r.add_argument("--action")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(fn=cmd_inspect)

    r = sub.add_parser("bundles", help="how bundling relates to 5x runners in your data (to set limits)")
    r.add_argument("dataset")
    r.add_argument("--checkpoint", default="10")
    r.set_defaults(fn=cmd_bundles)

    r = sub.add_parser("buys", help="bundle numbers of every buy the live run made")
    r.add_argument("--log", default="data/buys.csv")
    r.set_defaults(fn=cmd_buys)

    r = sub.add_parser("replay", help="run the live decider over archived events")
    r.add_argument("input", nargs="+")
    r.add_argument("--strategy", default="strategies/default.json")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(fn=cmd_replay)

    a = p.parse_args(argv)
    if getattr(a, "out", None) and a.cmd in ("train", "optimize", "export", "hunt"):
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
