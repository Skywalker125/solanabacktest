"""Record the Advanced Data Stream to hourly gzipped JSONL files.

Use this when you want your own archive (or the official hourly archive is
not available for the period you need).  Layout matches what the loader
expects:  <out>/YYYY-MM-DD/HH.jsonl.gz
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone

from .stream import STREAM_URL


class HourlyWriter:
    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        self._key = None
        self._fh = None
        self.count = 0
        self._flushed = time.time()

    def flush_if_due(self):
        # gzip buffers a lot; flush regularly so a crash or Ctrl+C loses little
        if self._fh and time.time() - self._flushed > 30:
            self._fh.flush()
            self._flushed = time.time()

    def write(self, ev: dict):
        ts = ev.get("timestamp") or int(time.time())
        if ts > 1e11:  # milliseconds
            ts //= 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        key = (dt.strftime("%Y-%m-%d"), dt.strftime("%H"))
        if key != self._key:
            self.close()
            d = os.path.join(self.out_dir, key[0])
            os.makedirs(d, exist_ok=True)
            # append mode: reconnects within the same hour keep writing the same file
            self._fh = gzip.open(os.path.join(d, f"{key[1]}.jsonl.gz"), "at", encoding="utf-8")
            self._key = key
        self._fh.write(json.dumps(ev, separators=(",", ":")))
        self._fh.write("\n")
        self.count += 1

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


def record(out_dir: str, protocols=None, actions=None, url: str = STREAM_URL, debug: bool = False):
    from .stream import run_stream

    w = HourlyWriter(out_dir)
    print(f"recording -> {os.path.abspath(out_dir)}", file=sys.stderr)
    try:
        run_stream(w.write, protocols, actions, url, debug=debug,
                   on_tick=w.flush_if_due, tick_s=5.0, status_every_s=30.0)
    finally:
        w.close()
        print(f"{w.count:,} events written", file=sys.stderr)
