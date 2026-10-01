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
    from datetime import datetime, timedelta, timezone
    from .fetch import fetch_range, parse_when
    template = a.url_template or os.environ.get("RUNNERBT_ARCHIVE_URL")
    if not template:
        sys.exit("no archive URL: pass --url-template (or set RUNNERBT_ARCHIVE_URL), e.g.\n"
                 "  --url-template \"https://<archive-host>/{date}/{HH}.jsonl.gz\"\n"
                 "copy the real pattern from the provider's 'Historical replay' docs")
    start = parse_when(a.start)
    end = parse_when(a.end) if a.end else start + timedelta(hours=_parse_duration(a.span) / 3600)
    end = min(end, datetime.now(timezone.utc))
    headers = dict(h.split(":", 1) for h in a.header or [])
    headers = {k.strip(): v.strip() for k, v in headers.items()}
    c = fetch_range(template, start, end, a.out, headers, workers=a.workers)
    if c["error"]:
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
        sol_only=a.sol_only,
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
    from .live import LiveDecider, print_decision, run_socket
    from .strategy import Strategy
    s = Strategy.load(a.strategy)
    dec = LiveDecider(s, print_decision, report_rejects=a.verbose)
    run_socket(dec, _csv_list(a.protocols), ["buy", "sell", "create", "createPool", "migrate",
                                             "curveComplete", "remove", "claimCreatorFees"], a.url)


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

    r = sub.add_parser("fetch", help="download the hourly historical archive for a time range")
    r.add_argument("--url-template", help="archive URL with {date} {HH} {yyyy} {mm} {dd} {hour} {unix} "
                                         "placeholders (or env RUNNERBT_ARCHIVE_URL)")
    r.add_argument("--from", dest="start", required=True, help="UTC start, e.g. 2026-09-01 or 2026-09-01T06")
    r.add_argument("--to", dest="end", help="UTC end (exclusive); default --from + --span")
    r.add_argument("--span", default="24h")
    r.add_argument("--out", default="data/raw")
    r.add_argument("--header", action="append", help="extra HTTP header, e.g. 'x-api-key: sk_...'")
    r.add_argument("--workers", type=int, default=4)
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
    r.add_argument("--sol-only", action="store_true")
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

    r = sub.add_parser("live", help="print live entry decisions (paper mode, no trading)")
    r.add_argument("--strategy", default="strategies/default.json")
    r.add_argument("--protocols")
    r.add_argument("--url", default="https://sol.shrine.trade")
    r.add_argument("-v", "--verbose", action="store_true", help="also print rejections")
    r.set_defaults(fn=cmd_live)

    r = sub.add_parser("replay", help="run the live decider over archived events")
    r.add_argument("input", nargs="+")
    r.add_argument("--strategy", default="strategies/default.json")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(fn=cmd_replay)

    a = p.parse_args(argv)
    if getattr(a, "out", None) and a.cmd in ("train", "optimize", "export"):
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
