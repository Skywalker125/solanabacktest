"""runnerbt - backtest and live entry decisions for Solana launchpad runners (5x+).

Pipeline:
    raw stream events (recorded live or from the hourly archive)
      -> ReplayEngine (per-token state, point-in-time feature snapshots, price paths)
      -> dataset of TokenRecords
      -> Strategy simulation / optimisation / scoring model
      -> LiveDecider (same engine + strategy on the live socket)
"""

__version__ = "0.1.0"

RUNNER_MULT = 5.0
SOL_MINT = "So11111111111111111111111111111111111111112"
