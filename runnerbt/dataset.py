"""Build, load and split the token-record dataset."""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from typing import Iterable

from .engine import EngineConfig, ReplayEngine
from .io import iter_events, read_jsonl


def build_dataset(inputs: Iterable[str], out_path: str, cfg: EngineConfig,
                  include_incomplete: bool = False, progress: bool = True) -> dict:
    """Replay archived events into a gzipped JSONL of token records."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    opener = gzip.open if out_path.endswith(".gz") else open
    stats = {"records": 0, "incomplete_skipped": 0}
    t_start = time.time()
    with opener(out_path, "wt", encoding="utf-8") as fh:
        def on_record(rec):
            if not rec["complete"] and not include_incomplete:
                stats["incomplete_skipped"] += 1
                return
            fh.write(json.dumps(rec, separators=(",", ":")))
            fh.write("\n")
            stats["records"] += 1

        eng = ReplayEngine(cfg, on_record=on_record)
        for ev in iter_events(inputs):
            eng.process(ev)
            if progress and eng.n_events % 500_000 == 0:
                print(f"  {eng.n_events:,} events, {len(eng.tokens):,} live tokens, "
                      f"{stats['records']:,} records", file=sys.stderr)
        eng.flush()
    stats["events"] = eng.n_events
    stats["seconds"] = round(time.time() - t_start, 1)
    return stats


def load_records(path: str, complete_only: bool = True) -> list[dict]:
    return [r for r in read_jsonl(path) if r.get("complete", True) or not complete_only]


def time_split(records: list[dict], train_frac: float = 0.7):
    """Chronological split: never train on the future."""
    rs = sorted(records, key=lambda r: r["created_ts"])
    k = int(len(rs) * train_frac)
    return rs[:k], rs[k:]
