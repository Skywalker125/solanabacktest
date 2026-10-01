"""Entry-only search: buy as many 5x runners as possible at a required precision.

Exits and PnL are ignored.  A launch is a *hit* if, after buying at the next trade after
the decision time (plus price impact of `position` and slippage), its price reaches
`mult` x the fill within the horizon.  For each checkpoint the search builds AND-rules of
feature thresholds with a beam search and, for each precision target, keeps the rule that
catches the most runners while its precision still clears the target with statistical
margin (Wilson lower bound) on the training period.  Every rule is then re-scored on the
later, unseen test period.  Protocol is never used: rules apply across all launchpads.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from . import RUNNER_MULT
from .model import BOOLEAN, NUMERIC
from .simulate import entry_impact, label

QUANTILES = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)


def wilson_lower(hits: int, n: int, z: float = 1.64) -> float:
    """One-sided ~95% lower bound of a proportion."""
    if n == 0:
        return 0.0
    p = hits / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - r) / d


def rows_for(records, cp: int, mult: float = RUNNER_MULT, position: float = 0.5, slippage_pct: float = 1.0,
             require: Optional[dict] = None, exclude=()):
    """(feature dicts, hit flags, max multiples) for launches tradable at checkpoint cp.

    `require` holds hard limits (strategy filter format): launches failing them are never
    bought, so they are left out of both learning and testing."""
    gate = None
    if require:
        from .strategy import Strategy
        gate = Strategy(filters=require)
    feats, ys, mm = [], [], []
    for r in records:
        if not r.get("complete", True):
            continue
        f = r.get("snapshots", {}).get(str(cp))
        e = r.get("entries", {}).get(str(cp))
        if not f or not e or not e[1]:
            continue
        if gate and not gate.passes_filters(f)[0]:
            continue
        m = label(r, cp, mult)["max_mult"] / entry_impact(e, position, slippage_pct)
        feats.append(f)
        ys.append(m >= mult)
        mm.append(m)
    return feats, ys, mm


@dataclass
class Cond:
    feature: str
    op: str      # ">=", "<=", "=="
    value: object

    def text(self) -> str:
        v = self.value
        return f"{self.feature} {self.op} {v:.4g}" if isinstance(v, float) else f"{self.feature} {self.op} {v}"


@dataclass
class Rule:
    cp: int
    conds: list
    train_n: int
    train_hits: int
    test_n: int = 0
    test_hits: int = 0
    test_runners: int = 0
    train_runners: int = 0
    target: float = 0.0
    kind: str = "rule"
    min_score: Optional[float] = None
    extra: dict = field(default_factory=dict)

    @property
    def train_precision(self):
        return self.train_hits / self.train_n if self.train_n else 0.0

    @property
    def test_precision(self):
        return self.test_hits / self.test_n if self.test_n else 0.0

    def text(self) -> str:
        if self.kind == "model":
            return f"model score >= {self.min_score:.3f}"
        return " AND ".join(c.text() for c in self.conds) or "(everything)"

    def filters(self) -> dict:
        out: dict = {}
        for c in self.conds:
            f = out.setdefault(c.feature, {})
            if c.op == ">=":
                f["min"] = max(f.get("min", -math.inf), c.value)
            elif c.op == "<=":
                f["max"] = min(f.get("max", math.inf), c.value)
            else:
                f["eq"] = c.value
        return out


def _candidates(np, feats, cols, exclude=()):
    """Threshold conditions from training quantiles, as (Cond, mask) pairs."""
    out = []
    for k in NUMERIC:
        if k in exclude:
            continue
        x = cols[k]
        qs = sorted({float(v) for v in np.quantile(x, QUANTILES)})
        for v in qs:
            ge = x >= v
            le = x <= v
            if 0 < ge.sum() < len(x):
                out.append((Cond(k, ">=", v), ge))
            if 0 < le.sum() < len(x):
                out.append((Cond(k, "<=", v), le))
    for k in BOOLEAN:
        if k in exclude:
            continue
        x = cols[k]
        for v in (True, False):
            m = x == v
            if 0 < m.sum() < len(x):
                out.append((Cond(k, "==", v), m))
    return out


def _columns(np, feats):
    cols = {}
    for k in NUMERIC:
        cols[k] = np.array([float(f.get(k) or 0.0) for f in feats])
    for k in BOOLEAN:
        cols[k] = np.array([bool(f.get(k)) for f in feats])
    return cols


def _apply(np, conds, cols, n):
    m = np.ones(n, dtype=bool)
    for c in conds:
        x = cols[c.feature]
        if c.op == ">=":
            m &= x >= c.value
        elif c.op == "<=":
            m &= x <= c.value
        else:
            m &= x == c.value
    return m


def beam_search(np, cands, y, targets, beam: int = 12, depth: int = 4, min_hits: int = 8):
    """Best rule per precision target: most hits with Wilson LB(precision) >= target."""
    n = len(y)
    best = {t: None for t in targets}
    frontier = [((), np.ones(n, dtype=bool))]
    seen = set()
    for _ in range(depth):
        scored = []
        for rule, mask in frontier:
            used = {(cands[i][0].feature, cands[i][0].op) for i in rule}
            for ci, (cond, cm) in enumerate(cands):
                if (cond.feature, cond.op) in used:
                    continue
                key = frozenset(rule + (ci,))
                if key in seen:
                    continue
                seen.add(key)
                m = mask & cm
                hits = int((m & y).sum())
                if hits < min_hits:
                    continue
                cnt = int(m.sum())
                lb = wilson_lower(hits, cnt)
                for t in targets:
                    if lb >= t and (best[t] is None or hits > best[t][1] or
                                    (hits == best[t][1] and cnt < best[t][2])):
                        best[t] = (rule + (ci,), hits, cnt)
                scored.append((hits, cnt, rule + (ci,), m))
        if not scored:
            break
        # keep a diverse beam: the best rules for each precision level, scored by
        # hits - lambda * misses with lambda the break-even odds of that target
        nxt = {}
        for t in targets:
            lam = t / (1 - t)
            top = sorted(scored, key=lambda s: -(s[0] - lam * (s[1] - s[0])))[:max(2, beam // len(targets))]
            for s in top:
                nxt[frozenset(s[2])] = (s[2], s[3])
        frontier = list(nxt.values())
    return best


def hunt(train_records, test_records, checkpoints, targets=(0.1, 0.2, 0.3, 0.4, 0.5),
         mult: float = RUNNER_MULT, position: float = 0.5, slippage_pct: float = 1.0,
         beam: int = 12, depth: int = 4, min_hits: int = 8, use_model: bool = True, log=print,
         require: Optional[dict] = None, exclude=()):
    import numpy as np

    results: list[Rule] = []
    base = {}
    for cp in checkpoints:
        f_tr, y_tr, _ = rows_for(train_records, cp, mult, position, slippage_pct, require)
        f_te, y_te, _ = rows_for(test_records, cp, mult, position, slippage_pct, require)
        if not f_tr or sum(y_tr) < min_hits:
            log(f"  {cp}s: not enough runners in training data ({sum(y_tr)})")
            continue
        y = np.array(y_tr)
        yt = np.array(y_te, dtype=bool)
        cols = _columns(np, f_tr)
        cols_te = _columns(np, f_te) if f_te else None
        base[cp] = {"train_n": len(y), "train_runners": int(y.sum()),
                    "test_n": len(yt), "test_runners": int(yt.sum())}
        log(f"  {cp:>4}s: {len(y):,} train launches, {int(y.sum())} runners ({y.mean():.2%}); "
            f"test {len(yt):,} / {int(yt.sum())}")
        cands = _candidates(np, f_tr, cols, set(exclude))
        best = beam_search(np, cands, y, targets, beam=beam, depth=depth, min_hits=min_hits)
        for t, b in best.items():
            if not b:
                continue
            idx, hits, cnt = b
            conds = [cands[i][0] for i in idx]
            r = Rule(cp, conds, cnt, hits, target=t, train_runners=int(y.sum()))
            if cols_te is not None:
                m = _apply(np, conds, cols_te, len(yt))
                r.test_n, r.test_hits = int(m.sum()), int((m & yt).sum())
            r.test_runners = int(yt.sum())
            results.append(r)
        if use_model:
            results += _model_rules(np, f_tr, y_tr, f_te, y_te, cp, targets, min_hits)
    return results, base


def _model_rules(np, f_tr, y_tr, f_te, y_te, cp, targets, min_hits):
    try:
        from .model import LogisticModel, vectorize
    except ImportError:
        return []
    try:
        m = LogisticModel.fit([vectorize(f) for f in f_tr], [float(v) for v in y_tr], checkpoint=cp)
    except ValueError:
        return []
    s_tr = np.array([m.predict_one(f) for f in f_tr])
    s_te = np.array([m.predict_one(f) for f in f_te]) if f_te else np.array([])
    y = np.array(y_tr, dtype=bool)
    yt = np.array(y_te, dtype=bool)
    order = np.argsort(-s_tr)
    cum_hits = np.cumsum(y[order])
    out = []
    for t in targets:
        best = None
        for k in range(min_hits, len(order) + 1):
            hits = int(cum_hits[k - 1])
            if hits >= min_hits and wilson_lower(hits, k) >= t and (best is None or hits > best[1]):
                best = (k, hits)
        if not best:
            continue
        k, hits = best
        thr = float(s_tr[order[k - 1]])
        r = Rule(cp, [], k, hits, target=t, kind="model", min_score=thr, train_runners=int(y.sum()))
        r.extra["model"] = m
        if len(s_te):
            mt = s_te >= thr
            r.test_n, r.test_hits = int(mt.sum()), int((mt & yt).sum())
        r.test_runners = int(yt.sum())
        out.append(r)
    return out


def best_per_target(results: list, by: str = "train") -> dict:
    """Pick, for each precision target, the rule that catches the most runners."""
    best: dict = {}
    for r in results:
        hits = r.train_hits if by == "train" else r.test_hits
        cur = best.get(r.target)
        if cur is None or hits > (cur.train_hits if by == "train" else cur.test_hits):
            best[r.target] = r
    return best


def format_frontier(results: list, base: dict) -> str:
    lines = []
    for t in sorted({r.target for r in results}):
        lines.append(f"\n== precision target {t:.0%} ==")
        lines.append(f"  {'cp':>5} {'kind':<6}{'train buys':>11}{'hits':>6}{'prec':>7}   "
                     f"{'test buys':>10}{'hits':>6}{'prec':>7}{'recall':>8}   rule")
        rs = sorted((r for r in results if r.target == t), key=lambda r: -r.train_hits)
        for r in rs:
            rec = r.test_hits / r.test_runners if r.test_runners else 0.0
            lines.append(f"  {r.cp:>4}s {r.kind:<6}{r.train_n:>11}{r.train_hits:>6}{r.train_precision:>7.1%}   "
                         f"{r.test_n:>10}{r.test_hits:>6}{r.test_precision:>7.1%}{rec:>8.1%}   {r.text()}")
    return "\n".join(lines)
