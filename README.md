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
# 1a. historical data: Shrine's free hourly archive (https://replay.shrine.trade/pump/YYYY/MM/DD/HH.jsonl.zst)
python -m runnerbt fetch --from 2026-09-20 --to 2026-09-27 --slim     # a week -> data/slim
python -m runnerbt fetch --last 48 --slim                              # newest 48 hours in the archive
python -m runnerbt fetch --all                                         # everything, raw -> data/raw
#     --slim keeps launches + their lifecycle + trades on tokens launched in the last --keep-hours (24)
#     and drops unused fields; raw hours are a few hundred MB each and are deleted after slimming.
#     Re-running resumes: finished hours are skipped, half-downloaded files continue.
# 1b. and/or record the live stream yourself (same format)
python -m runnerbt record --out data/raw          # --debug prints the raw Socket.IO traffic

# 2. replay into a dataset: features at 10s/30s/1m/2m/5m after launch, then follow each token for 6h
python -m runnerbt build data/slim --out data/dataset.jsonl.gz --checkpoints 10,30,60,120,300 --horizon 6h

# 3. base rates: how many launches go 5x+ from each entry time, per protocol
python -m runnerbt stats data/dataset.jsonl.gz

# 4. THE MAIN STEP - find the entry rule that buys the most 5x runners at a required precision,
#    across all launchpads (protocol is never used). Exits/PnL are ignored: a buy is a hit if the
#    price reaches 5x of our actual fill (incl. price impact + slippage) within the horizon.
python -m runnerbt hunt data/dataset.jsonl.gz --precision 0.3 --require anti-bundle --out strategies/hunt.json
#    --require sets hard limits that are never crossed, whatever the score. anti-bundle =
#    early_slots_pct<=10 (% of supply bought in the launch block + 2 slots after),
#    bundle_slot_pct<=10 (% bought in slots where 3+ wallets bought together),
#    max_slot_buyers<=3, top10_hold_pct<=35 (net holdings of the top 10 wallets), dev_hold_pct<=8.
#    Pick your own limits from the data first:
python -m runnerbt bundles data/dataset.jsonl.gz --checkpoint 10
#    and check what live bought:  python -m runnerbt buys
#    prints, for each precision target (10..50%) and each decision time, the best rule and the best
#    model threshold: buys / runners / precision on the learning period AND on the later unseen period.
#    Rules must clear the target with a statistical margin (Wilson lower bound) and catch >= --min-hits.

# 4b. backtest a strategy (in-sample vs. later out-of-sample, trades to CSV)
python -m runnerbt backtest data/dataset.jsonl.gz --strategy strategies/default.json --split 0.7 --trades data/trades.csv

# 5. search filters + exits on the first 70% of time, validate on the last 30%
python -m runnerbt optimize data/dataset.jsonl.gz --iters 1000 --out strategies/optimized.json

# 6. optional: P(5x) scoring model, then use it as a gate in a strategy ("model" + "min_score")
python -m runnerbt train data/dataset.jsonl.gz --checkpoint 30 --out models/model.json

# 7. run the chosen strategy live: every fired buy is appended to data/buys.csv
#    (time_local, time_utc, unix, mint, symbol, name, protocol, launch_utc, age_s, mcap_usd, mcap_sol, price, score)
#    Only tokens launched after it connected are decided; checks wait for the stream's delay.
#    data/buys_raw.jsonl keeps the raw launch + last trade event of every buy.
#    Reconnects by itself (also when the server refuses a duplicate connection or goes silent).
# check what each launchpad reports as quote currency / launch market cap:
python -m runnerbt inspect data/slim --quotes
python -m runnerbt live --strategy strategies/hunt.json --warmup data/slim
#    --warmup replays your downloaded archive first so creator-history features match the backtest.
#    No orders are sent.

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
