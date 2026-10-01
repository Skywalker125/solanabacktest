"""Strategy definition: entry filters, optional model score, exits and costs.

A strategy is plain JSON so the app can store, version and hot-swap it:

{
  "name": "default",
  "checkpoints": [30, 60],          # evaluate at these ages; enter at the first that passes
  "filters": {"n_buys": {"min": 15}, "dev_sold": {"eq": false},
              "protocol": {"in": ["PUMPFUN", "BONK"]}},
  "model": "models/model.json",     # optional scoring model
  "min_score": 0.08,                # required P(5x) if a model is set
  "exit": {"take_profit": [{"mult": 5, "frac": 0.6}, {"mult": 10, "frac": 1.0}],
           "stop_loss": 0.5, "trailing": {"activate": 3.0, "drop": 0.4},
           "max_hold_s": 21600},
  "position": 0.5, "fee_pct": 1.0, "slippage_pct": 1.0
}
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

DEFAULT_EXIT = {
    "take_profit": [{"mult": 5.0, "frac": 0.6}, {"mult": 10.0, "frac": 1.0}],
    "stop_loss": 0.5,
    "trailing": None,
    "max_hold_s": 6 * 3600,
}


@dataclass
class Strategy:
    name: str = "unnamed"
    checkpoints: list = field(default_factory=lambda: [60])
    filters: dict = field(default_factory=dict)
    model: Optional[str] = None
    min_score: Optional[float] = None
    exit: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_EXIT))
    position: float = 0.5        # quote units (SOL) per entry
    fee_pct: float = 1.0         # per side, launchpad/AMM + priority fees
    slippage_pct: float = 1.0    # per side, on top of modelled price impact
    _model_obj: Any = field(default=None, repr=False, compare=False)

    # ----------------------------------------------------------------- io
    @classmethod
    def from_dict(cls, d: dict) -> "Strategy":
        d = dict(d)
        if "checkpoint" in d and "checkpoints" not in d:
            d["checkpoints"] = [d.pop("checkpoint")]
        ex = copy.deepcopy(DEFAULT_EXIT)
        ex.update(d.pop("exit", {}) or {})
        known = {k: d[k] for k in ("name", "checkpoints", "filters", "model", "min_score",
                                   "position", "fee_pct", "slippage_pct") if k in d}
        s = cls(exit=ex, **known)
        s.checkpoints = sorted(int(c) for c in s.checkpoints)
        return s

    @classmethod
    def load(cls, path: str) -> "Strategy":
        with open(path) as fh:
            s = cls.from_dict(json.load(fh))
        # a model path that doesn't exist from the current folder: try next to the strategy file
        if s.model and not os.path.exists(s.model):
            alt = os.path.join(os.path.dirname(os.path.abspath(path)), os.path.basename(s.model))
            if os.path.exists(alt):
                s.model = alt
        return s

    def to_dict(self) -> dict:
        return {
            "name": self.name, "checkpoints": self.checkpoints, "filters": self.filters,
            "model": self.model, "min_score": self.min_score, "exit": self.exit,
            "position": self.position, "fee_pct": self.fee_pct, "slippage_pct": self.slippage_pct,
        }

    def save(self, path: str):
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")

    # ------------------------------------------------------------- decide
    def get_model(self):
        if self.model and self._model_obj is None:
            from .model import LogisticModel
            self._model_obj = LogisticModel.load(self.model)
        return self._model_obj

    def passes_filters(self, feat: dict) -> tuple[bool, Optional[str]]:
        for key, rule in self.filters.items():
            v = feat.get(key)
            if not isinstance(rule, dict):
                rule = {"eq": rule}
            if v is None:
                return False, f"{key} missing"
            if "eq" in rule and v != rule["eq"]:
                return False, f"{key}={v} != {rule['eq']}"
            if "in" in rule and v not in rule["in"]:
                return False, f"{key}={v} not in {rule['in']}"
            if "not_in" in rule and v in rule["not_in"]:
                return False, f"{key}={v} excluded"
            if rule.get("min") is not None and v < rule["min"]:
                return False, f"{key}={v:.4g} < {rule['min']}"
            if rule.get("max") is not None and v > rule["max"]:
                return False, f"{key}={v:.4g} > {rule['max']}"
        return True, None

    def score(self, feat: dict) -> Optional[float]:
        m = self.get_model()
        return m.predict_one(feat) if m else None

    def decide(self, feat: dict) -> dict:
        """Entry decision for one feature snapshot. Used by both backtest and live."""
        ok, reason = self.passes_filters(feat)
        score = None
        if ok and self.get_model() is not None:
            score = self.score(feat)
            if self.min_score is not None and score < self.min_score:
                ok, reason = False, f"score {score:.3f} < {self.min_score}"
        return {"enter": ok, "reason": reason, "score": score}
