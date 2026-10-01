"""Small logistic-regression scorer for P(5x | snapshot).

Kept dependency-light on purpose: training needs numpy, prediction is pure
Python so the live decider can run anywhere.  The model is a JSON file.
"""

from __future__ import annotations

import json
import math
from typing import Iterable, Optional

from . import RUNNER_MULT
from .simulate import label

NUMERIC = [
    "creator_prev_launches", "creator_prev_runners", "creator_open_launches",
    "dev_initial_buy", "dev_initial_pct", "dev_sold_pct",
    "n_buys", "n_sells", "buy_sell_ratio", "unique_buyers", "unique_sellers", "unique_signers",
    "buy_vol", "sell_vol", "net_flow", "flow_30s", "flow_60s", "buys_30s", "avg_buy",
    "top1_share", "top3_share", "bundle_buyers", "bundle_share", "launch_block_pct",
    "early_slots_pct", "bundle_slot_pct", "max_slot_buyers", "top10_hold_pct", "dev_hold_pct", "holders",
    "unbought_sell_pct", "unbought_sellers", "off_curve_pct", "launch_mcap",
    "liq_added", "liq_mcap_ratio", "liq_drop_pct",
    "mcap", "max_mcap", "quote_in_pool", "mult_from_launch", "drawdown_from_max",
    "liq_removes", "fee_claims",
]
BOOLEAN = ["quote_is_sol", "has_authority", "risky_extension", "dev_sold", "migrated", "curve_complete"]
# Deliberately empty: the scorer is protocol-agnostic, so one model ranks launches from every
# launchpad by behaviour (flow, holders, dev) rather than by where they launched.
CATEGORICAL: dict = {}


def _signed_log(x: float) -> float:
    return math.copysign(math.log1p(abs(x)), x)


def vectorize(feat: dict) -> list[float]:
    v = [_signed_log(float(feat.get(k) or 0.0)) for k in NUMERIC]
    v += [1.0 if feat.get(k) else 0.0 for k in BOOLEAN]
    for k, cats in CATEGORICAL.items():
        v += [1.0 if feat.get(k) == c else 0.0 for c in cats]
    return v


def feature_names() -> list[str]:
    names = list(NUMERIC) + list(BOOLEAN)
    for k, cats in CATEGORICAL.items():
        names += [f"{k}={c}" for c in cats]
    return names


def build_xy(records: Iterable[dict], cp: int, mult: float = RUNNER_MULT):
    X, y, meta = [], [], []
    for r in records:
        if not r.get("complete", True):
            continue
        feat = r.get("snapshots", {}).get(str(cp))
        lab = label(r, cp, mult)
        if not feat or not lab:
            continue
        X.append(vectorize(feat))
        y.append(1.0 if lab["runner"] else 0.0)
        meta.append(r["created_ts"])
    return X, y, meta


class LogisticModel:
    def __init__(self, names, mean, std, weights, bias, checkpoint=None, mult=RUNNER_MULT, info=None):
        self.names, self.mean, self.std = names, mean, std
        self.weights, self.bias = weights, bias
        self.checkpoint, self.mult, self.info = checkpoint, mult, info or {}

    def predict_one(self, feat: dict) -> float:
        return self.predict_vec(vectorize(feat))

    def predict_vec(self, x: list[float]) -> float:
        z = self.bias
        for xi, m, s, w in zip(x, self.mean, self.std, self.weights):
            z += w * (xi - m) / s
        return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))

    @classmethod
    def fit(cls, X, y, l2: float = 1.0, iters: int = 400, lr: float = 0.5,
            checkpoint: Optional[int] = None, mult: float = RUNNER_MULT) -> "LogisticModel":
        import numpy as np

        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        mean = X.mean(axis=0)
        std = X.std(axis=0)
        std[std == 0] = 1.0
        Z = (X - mean) / std
        pos = y.sum()
        if pos == 0:
            raise ValueError("no positive (5x) examples to learn from")
        # balance classes so the rare runners are not ignored
        w_pos = len(y) / (2 * pos)
        w_neg = len(y) / (2 * (len(y) - pos))
        sw = np.where(y > 0, w_pos, w_neg)
        w = np.zeros(Z.shape[1])
        b = 0.0
        n = len(y)
        for _ in range(iters):
            p = 1.0 / (1.0 + np.exp(-np.clip(Z @ w + b, -40, 40)))
            g = sw * (p - y)
            w -= lr * ((Z.T @ g) / n + l2 * w / n)
            b -= lr * g.mean()
        # the balanced fit outputs inflated probabilities; recalibrate the bias to the base rate
        prior = pos / n
        b += math.log(prior / (1 - prior)) - math.log(0.5 / 0.5)
        return cls(feature_names(), mean.tolist(), std.tolist(), w.tolist(), float(b),
                   checkpoint=checkpoint, mult=mult, info={"n": n, "positives": int(pos)})

    def top_weights(self, k: int = 12):
        return sorted(zip(self.names, self.weights), key=lambda t: -abs(t[1]))[:k]

    def save(self, path: str):
        with open(path, "w") as fh:
            json.dump({"names": self.names, "mean": self.mean, "std": self.std, "weights": self.weights,
                       "bias": self.bias, "checkpoint": self.checkpoint, "mult": self.mult,
                       "info": self.info}, fh)

    @classmethod
    def load(cls, path: str) -> "LogisticModel":
        with open(path) as fh:
            d = json.load(fh)
        if d["names"] != feature_names():
            raise ValueError(f"model {path} was trained on a different feature set; retrain it")
        return cls(d["names"], d["mean"], d["std"], d["weights"], d["bias"],
                   d.get("checkpoint"), d.get("mult", RUNNER_MULT), d.get("info"))


def auc(scores: list[float], y: list[float]) -> float:
    pairs = sorted(zip(scores, y))
    pos = sum(y)
    neg = len(y) - pos
    if not pos or not neg:
        return float("nan")
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        rank_sum += avg_rank * sum(1 for k in range(i, j + 1) if pairs[k][1] > 0)
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)
