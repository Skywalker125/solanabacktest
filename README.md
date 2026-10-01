# runnerbt — Solana launchpad runner backtester

This backtests entry rules for new launches on every launchpad in the shrine.trade
**Advanced Data Stream** (PumpFun, PumpFun Mayhem, Bonk, StonkFun, Meteora DBC, plus pool launches on
PumpSwap/Raydium/Meteora/Orca). It looks only for **runners that reach 5x or more** after your entry.
The same engine and strategy files run live, so the app's "new launches" screen can make the
backtested entry decision in real time.

```
stream events ──► ReplayEngine ──► token records ──► backtest / optimise / train
 (archive or        point-in-time      (dataset)          │
  live socket)      features + path                       ▼
                         ▲                         strategy.json (+ model.json)
                         └──────────── LiveDecider ◄──────┘   → "enter / skip" per launch
```

## Install

```bash
pip install -r requirements.txt     # core is stdlib-only; socketio for live/record, numpy for train
```

## Workflow

```bash
# 1. get data: record the stream yourself (hourly files under data/raw/YYYY-MM-DD/HH.jsonl.gz) ...
python -m runnerbt record --out data/raw
#    ... and/or download the hourly "Historical replay" archive into a folder
#    (.jsonl, .jsonl.gz, .ndjson or .json arrays are all accepted)

# 2. replay into a dataset: features at 10s/30s/1m/2m/5m after launch, then follow each token for 6h
python -m runnerbt build data/raw --out data/dataset.jsonl.gz --checkpoints 10,30,60,120,300 --horizon 6h

# 3. base rates: how many launches go 5x+ from each entry time, per protocol
python -m runnerbt stats data/dataset.jsonl.gz

# 4. backtest a strategy (in-sample vs. later out-of-sample, trades to CSV)
python -m runnerbt backtest data/dataset.jsonl.gz --strategy strategies/default.json --split 0.7 --trades data/trades.csv

# 5. search filters + exits on the first 70% of time, validate on the last 30%
python -m runnerbt optimize data/dataset.jsonl.gz --iters 1000 --out strategies/optimized.json

# 6. optional: P(5x) scoring model, then use it as a gate in a strategy ("model" + "min_score")
python -m runnerbt train data/dataset.jsonl.gz --checkpoint 30 --out models/model.json

# 7. paper-trade live: prints enter decisions for new launches (no orders are sent)
python -m runnerbt live --strategy strategies/optimized.json

# what would the live decider have flagged on archived data?
python -m runnerbt replay data/raw --strategy strategies/optimized.json
```

To try it without real data, run `python -m runnerbt synth --out data/synth`. It generates synthetic
events. They are only for checking that the pipeline runs, and results on them say nothing about the market.

## How the backtest stays honest

- **Point in time.** A snapshot at checkpoint `cp` sees only events with `timestamp <= created + cp`.
  The entry fills on the **next trade after** the decision time, never on a past price.
- **Creator history** (`creator_prev_launches`, `creator_prev_runners`) counts only launches whose horizon
  had already ended at that moment, so it can't leak future outcomes.
- **Costs.** Fees and slippage are charged on each side. Price impact uses constant-product math on the pool's
  `quoteInPool`, so 1 SOL into a 30 SOL curve costs about 3% extra.
- **Exits.** Take-profit levels fill at the target. Stops and trailing stops fill at the **observed** price,
  so a rug that gaps from 1x to 0.05x loses about 95%, not the stop distance.
- **Price paths** are compressed to a point every ≥1% move (`--path-step`), and the last price is always kept.
- **Validation.** `optimize` and `train` split by time, and the report shows out-of-sample numbers.
  Trust only those.
- `tests/test_runnerbt.py` checks for no lookahead, trades that arrive before their create, gap-through stops,
  and that the **live decider flags every token the backtest enters**.

## Features per snapshot

Safety: `has_authority`, `risky_extension` (transferFee / permanentDelegate / …), `quote_is_sol`.
Dev: `dev_initial_buy`, `dev_initial_pct`, `dev_sold`, `dev_sold_pct`, `creator_prev_launches`,
`creator_prev_runners`, `creator_open_launches`.
Flow: `n_buys`, `n_sells`, `buy_sell_ratio`, `unique_buyers`, `unique_sellers`, `unique_signers`, `buy_vol`,
`sell_vol`, `net_flow`, `flow_30s`, `flow_60s`, `buys_30s`, `avg_buy`.
Distribution: `top1_share`, `top3_share`, `bundle_buyers`/`bundle_share` (buys in the launch block).
Price: `mcap`, `max_mcap`, `quote_in_pool`, `mult_from_launch`, `drawdown_from_max`.
Lifecycle: `migrated`, `curve_complete`, `liq_removes`, `fee_claims`, `protocol`, `launch_type`, `age_s`.

Run `python -m runnerbt export data/dataset.jsonl.gz --checkpoint 30` to get them as a CSV with labels for your own analysis.

## Strategy file

See `strategies/default.json`. Filters support `min`, `max`, `eq`, `in` and `not_in` on any feature.
`checkpoints` lists the decision times: the strategy enters at the first one that passes and decides only
once per token. Exits support take-profit ladders, a stop (a fraction of the entry price), a trailing stop
that turns on at an `activate` multiple, and `max_hold_s`.

## Using it from the app

```python
from runnerbt.live import LiveDecider
from runnerbt.strategy import Strategy

decider = LiveDecider(Strategy.load("strategies/optimized.json"), on_decision=lambda d: ...)
# for every `stream` socket event:
decider.process(event)
# once a second, so checkpoints fire even when the market is quiet:
decider.tick()
```

Each decision includes `mint`, `symbol`, `protocol`, `checkpoint`, `enter`, `reason` (why a token was
rejected), `score`, the full `features` snapshot, and the strategy's `exit_plan`.
