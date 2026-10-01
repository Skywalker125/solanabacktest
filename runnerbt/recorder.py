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

from .live import STREAM_URL


class HourlyWriter:
    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        self._key = None
        self._fh = None
        self.count = 0

    def write(self, ev: dict):
        ts = ev.get("timestamp") or int(time.time())
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


def record(out_dir: str, protocols=None, actions=None, url: str = STREAM_URL):
    import socketio

    w = HourlyWriter(out_dir)
    sio = socketio.Client(reconnection=True)

    @sio.event
    def connect():
        payload = {}
        if protocols:
            payload["protocols"] = list(protocols)
        if actions:
            payload["actions"] = list(actions)
        sio.emit("subscribe_stream", payload)
        print(f"recording {url} -> {out_dir} ({payload or 'all'})", file=sys.stderr)

    @sio.on("stream")
    def on_stream(ev):
        w.write(ev)

    sio.connect(url, transports=["websocket"])
    last = time.time()
    try:
        while True:
            sio.sleep(5)
            if time.time() - last > 60:
                print(f"  {w.count:,} events recorded", file=sys.stderr)
                last = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        w.close()
        sio.disconnect()
