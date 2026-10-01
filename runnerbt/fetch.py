"""Download the Shrine historical replay archive for backtesting.

The archive (free, keyless) holds every Advanced Data Stream event, one file per UTC hour:

    https://replay.shrine.trade/pump/YYYY/MM/DD/HH.jsonl.zst
    https://replay.shrine.trade/pump/index.json      -> {"base", "hours": ["2026/09/11/07", ...]}

Each line is exactly the live `stream` event plus `localTimestamp` (ms).  An hour is a few
hundred MB compressed, so there are two ways to keep it:

  raw   the .jsonl.zst file as published (everything, every protocol)
  slim  only what a launch backtest needs: launches (create / new pools), lifecycle events, and
        trades on tokens launched in the last `keep_hours`, with unused fields dropped,
        written as .jsonl.zst (.jsonl.gz without zstandard).  Typically a fraction of the raw size.

Files land in <out>/YYYY/MM/DD/HH.<ext> (the same layout as the archive), which `build`
reads in chronological order.  Finished files are skipped, so a re-run resumes.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

from .io import loads, open_zstd_text

ARCHIVE_BASE = os.environ.get("RUNNERBT_ARCHIVE_BASE", "https://replay.shrine.trade/pump").rstrip("/")
INDEX_URL = ARCHIVE_BASE + "/index.json"
DEFAULT_TEMPLATE = ARCHIVE_BASE + "/{yyyy}/{mm}/{dd}/{HH}.jsonl.zst"
CHUNK = 1 << 20


# ---------------------------------------------------------------- time helpers
def parse_when(s: str) -> datetime:
    s = s.strip().replace(" ", "T").rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d", "%Y/%m/%d/%H"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=timezone.utc)
    raise ValueError(f"can't parse time {s!r} (use 2026-09-30 or 2026-09-30T07)")


def hours_between(start: datetime, end: datetime):
    t = start.replace(minute=0, second=0, microsecond=0)
    while t < end:
        yield t
        t += timedelta(hours=1)


def hour_key(t: datetime) -> str:
    return t.strftime("%Y/%m/%d/%H")


def render(template: str, t: datetime) -> str:
    unix = int(t.timestamp())
    return template.format(date=t.strftime("%Y-%m-%d"), yyyy=t.strftime("%Y"), mm=t.strftime("%m"),
                           dd=t.strftime("%d"), HH=t.strftime("%H"), hour=t.hour, unix=unix,
                           unix_ms=unix * 1000, iso=t.strftime("%Y-%m-%dT%H:%M:%SZ"))


# ---------------------------------------------------------------------- http
def _request(url: str, headers: Optional[dict] = None, offset: int = 0):
    h = {"User-Agent": "runnerbt/0.2", **(headers or {})}
    if offset:
        h["Range"] = f"bytes={offset}-"
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60)


def load_index(url: str = INDEX_URL, headers: Optional[dict] = None) -> list[str]:
    """Hours the archive holds, oldest first, as 'YYYY/MM/DD/HH'."""
    with _request(url, headers) as resp:
        data = json.loads(resp.read())
    return list(data.get("hours") or [])


def download(url: str, dest: str, headers: Optional[dict] = None, retries: int = 5,
             progress: Optional[callable] = None) -> int:
    """Stream `url` to `dest` (via dest.part, resuming a partial download). Returns bytes."""
    part = dest + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    delay = 2.0
    last_err = None
    for attempt in range(retries + 1):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        try:
            with _request(url, headers, offset=have) as resp:
                if have and resp.status != 206:  # server ignored the range: start over
                    have = 0
                total = have + int(resp.headers.get("Content-Length") or 0)
                with open(part, "ab" if have else "wb") as fh:
                    got = have
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        if progress:
                            progress(got, total)
            if total and got < total:
                raise ConnectionError(f"short read {got}/{total} bytes")
            os.replace(part, dest)
            return got
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                raise FileNotFoundError(url) from None
            if e.code == 416 and have:  # range past the end: the .part is complete
                os.replace(part, dest)
                return have
            last_err = f"HTTP {e.code}"
            if e.code == 429:
                delay = max(delay, float(e.headers.get("Retry-After") or 10))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = str(e) or type(e).__name__
        if attempt < retries:
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise ConnectionError(f"{url}: {last_err}")


# ---------------------------------------------------------------------- slim
TRADE_KEYS = ("signature", "block", "timestamp", "action", "protocol", "txSigner", "pool", "mint",
              "quoteMint", "tokenAmount", "quoteAmount", "price", "marketCapQuote", "tokensInPool",
              "quoteInPool", "tradersInvolved")
LEG_KEYS = ("trader", "action", "tokenAmount", "quoteAmount")
LIFECYCLE = {"migrate", "curveComplete", "remove", "add", "claimCreatorFees"}


class Slimmer:
    """Keeps launch-relevant events. State carries across hours (save/load between runs)."""

    def __init__(self, keep_hours: float = 24.0, protocols: Optional[set] = None):
        self.keep_s = int(keep_hours * 3600)
        self.protocols = protocols
        self.launches: dict[str, int] = {}   # mint -> launch ts
        self.pools: dict[str, str] = {}      # pool -> mint
        self._order: deque = deque()          # (ts, mint) for expiry
        self._sig = None
        self._pending: list = []
        self.last_hour: Optional[str] = None
        self.kept = 0
        self.seen = 0

    # state survives between separate fetch runs over consecutive hours
    def save(self, path: str):
        with open(path, "w") as fh:
            json.dump({"last_hour": self.last_hour, "keep_s": self.keep_s, "launches": self.launches,
                       "pools": self.pools}, fh)

    def load(self, path: str):
        with open(path) as fh:
            d = json.load(fh)
        self.last_hour = d.get("last_hour")
        self.launches = {k: int(v) for k, v in d.get("launches", {}).items()}
        self.pools = d.get("pools", {})
        self._order = deque(sorted((ts, m) for m, ts in self.launches.items()))

    def _expire(self, now: int):
        cutoff = now - self.keep_s
        while self._order and self._order[0][0] < cutoff:
            _ts, mint = self._order.popleft()
            if self.launches.get(mint, now) < cutoff:
                self.launches.pop(mint, None)
        if len(self.pools) > 4 * max(1, len(self.launches)) + 1000:
            self.pools = {p: m for p, m in self.pools.items() if m in self.launches}

    def _add_launch(self, ev: dict) -> bool:
        mint = ev.get("mint")
        if not mint or mint in self.launches:
            return False
        if self.protocols and ev.get("protocol") not in self.protocols:
            return False
        ts = int(ev.get("timestamp") or 0)
        self.launches[mint] = ts
        self._order.append((ts, mint))
        if ev.get("pool"):
            self.pools[ev["pool"]] = mint
        return True

    @staticmethod
    def _slim_trade(ev: dict) -> dict:
        out = {k: ev[k] for k in TRADE_KEYS if k in ev}
        legs = ev.get("breakdown")
        if legs and len(legs) > 1:
            out["breakdown"] = [{k: leg[k] for k in LEG_KEYS if k in leg} for leg in legs]
        elif legs:
            # single leg: the top-level fields already say it all except the trader
            out["breakdown"] = [{k: legs[0][k] for k in LEG_KEYS if k in legs[0]}]
        return out

    def feed(self, ev: dict) -> list[dict]:
        """Returns the events to keep (possibly including buffered ones)."""
        self.seen += 1
        out: list = []
        sig = ev.get("signature")
        if sig != self._sig:
            self._sig, self._pending = sig, []
        ts = ev.get("timestamp")
        if ts and self.seen % 50_000 == 0:
            self._expire(int(ts))
        action = ev.get("action")
        if action in ("buy", "sell"):
            mint = ev.get("mint") or self.pools.get(ev.get("pool"))
            if mint in self.launches:
                out.append(self._slim_trade(ev))
            else:
                self._pending.append(ev)  # the create may follow in the same transaction
        elif action == "create" or (action == "createPool" and ev.get("mint")):
            known = ev.get("mint") in self.launches
            if self._add_launch(ev):
                mint = ev["mint"]
                out.extend(self._slim_trade(p) for p in self._pending
                           if p.get("mint") == mint or p.get("pool") == ev.get("pool"))
                ev = dict(ev)
                ev.pop("uri", None)
                out.append(ev)
            elif known and ev.get("pool"):
                self.pools[ev["pool"]] = ev["mint"]
                out.append(ev)
        elif action in LIFECYCLE:
            mint = ev.get("mint") or self.pools.get(ev.get("pool")) or self.pools.get(ev.get("fromPool"))
            if mint in self.launches:
                if action == "migrate" and ev.get("toPool"):
                    self.pools[ev["toPool"]] = mint
                out.append(ev)
        self.kept += len(out)
        return out


def _open_writer(part: str):
    """zstd text writer when available (smaller, faster), else gzip."""
    try:
        import io
        import zstandard
        raw = open(part, "wb")
        return io.TextIOWrapper(zstandard.ZstdCompressor(level=6).stream_writer(raw, closefd=True),
                                encoding="utf-8")
    except ImportError:
        return gzip.open(part, "wt", encoding="utf-8", compresslevel=5)


def slim_ext() -> str:
    try:
        import zstandard  # noqa: F401
        return ".jsonl.zst"
    except ImportError:
        return ".jsonl.gz"


def slim_file(src: str, dest: str, slimmer: Slimmer) -> tuple[int, int]:
    seen0, kept0 = slimmer.seen, slimmer.kept
    part = dest + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with open(src, "rb") as raw, _open_writer(part) as out:
        for line in open_zstd_text(raw):
            if not line.strip():
                continue
            for ev in slimmer.feed(loads(line)):
                out.write(json.dumps(ev, separators=(",", ":")))
                out.write("\n")
    os.replace(part, dest)
    return slimmer.seen - seen0, slimmer.kept - kept0


# ---------------------------------------------------------------------- main
def plan_hours(start: Optional[datetime], end: Optional[datetime], last: Optional[int],
               use_index: bool, template: str, headers: Optional[dict]) -> list[datetime]:
    hours: Optional[list[datetime]] = None
    if use_index:
        try:
            idx = load_index(headers=headers)
            hours = [parse_when(h) for h in idx]
            print(f"archive index: {len(hours)} hours, {idx[0]} .. {idx[-1]} UTC" if idx else
                  "archive index is empty", file=sys.stderr)
        except Exception as e:  # fall back to computing the hours
            print(f"could not read {INDEX_URL} ({e}); computing hours from the range", file=sys.stderr)
    if hours is None:
        if start is None:
            raise SystemExit("--from is required when the archive index is not used")
        hours = list(hours_between(start, end or datetime.now(timezone.utc) - timedelta(hours=1)))
    if start:
        hours = [h for h in hours if h >= start]
    if end:
        hours = [h for h in hours if h < end]
    if last:
        hours = hours[-last:]
    return hours


def fetch(out_dir: str, start: Optional[datetime] = None, end: Optional[datetime] = None,
          last: Optional[int] = None, slim: bool = False, keep_hours: float = 24.0,
          protocols: Optional[set] = None, keep_raw: bool = False, workers: int = 3,
          template: str = DEFAULT_TEMPLATE, use_index: bool = True, headers: Optional[dict] = None) -> dict:
    hours = plan_hours(start, end, last, use_index and template == DEFAULT_TEMPLATE, template, headers)
    if not hours:
        print("nothing to fetch for that range", file=sys.stderr)
        return {"ok": 0}
    raw_dir = os.path.join(out_dir, "_raw") if slim else out_dir
    state_path = os.path.join(out_dir, "_slim_state.json")
    print(f"{len(hours)} hours {hour_key(hours[0])} .. {hour_key(hours[-1])} UTC -> {os.path.abspath(out_dir)}"
          f"{' (slim)' if slim else ''}", file=sys.stderr)

    slimmer = None
    if slim:
        slimmer = Slimmer(keep_hours, protocols)
        if os.path.exists(state_path):
            # continue the launch state of the previous run (hours already slimmed are skipped)
            slimmer.load(state_path)
            prev = parse_when(slimmer.last_hour) if slimmer.last_hour else None
            new = [h for h in hours if prev is None or h > prev]
            if prev and new and new[0] - prev > timedelta(hours=1):
                print(f"note: gap after the previous run ({slimmer.last_hour}); trades of tokens launched "
                      f"before the gap are not kept", file=sys.stderr)

    def final_path(t):
        return os.path.join(out_dir, *hour_key(t).split("/")) + (slim_ext() if slim else ".jsonl.zst")

    def raw_path(t):
        return os.path.join(raw_dir, *hour_key(t).split("/")) + ".jsonl.zst"

    def get(t):
        if os.path.exists(final_path(t)) or os.path.exists(raw_path(t)):
            return "have"
        try:
            n = download(render(template, t), raw_path(t), headers)
            return f"{n / 1e6:.0f} MB"
        except FileNotFoundError:
            return "missing"

    counts = {"ok": 0, "skipped": 0, "missing": 0, "error": 0}
    t0 = time.time()
    window = max(1, workers) + 1  # bounded read-ahead: raw hours are big
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending: deque = deque()
        queue = iter(hours)

        def top_up():
            while len(pending) < window:
                t = next(queue, None)
                if t is None:
                    return
                pending.append((t, pool.submit(get, t)))

        top_up()
        i = 0
        # downloads run ahead in parallel; results are handled strictly in time order so the
        # slimmer sees the stream in sequence
        while pending:
            t, f = pending.popleft()
            i += 1
            try:
                res = f.result()
            except Exception as e:
                counts["error"] += 1
                print(f"  [{i}/{len(hours)}] {hour_key(t)}  ERROR {e}", file=sys.stderr)
                if counts["error"] >= 5 and counts["ok"] == 0:
                    print("5 errors and nothing downloaded: check your connection", file=sys.stderr)
                    for _t, ff in pending:
                        ff.cancel()
                    break
                top_up()
                continue
            top_up()
            if res == "missing":
                counts["missing"] += 1
                print(f"  [{i}/{len(hours)}] {hour_key(t)}  not in archive", file=sys.stderr)
                continue
            if res == "have" and os.path.exists(final_path(t)):
                counts["skipped"] += 1
                continue
            msg = res
            if slimmer:
                seen, kept = slim_file(raw_path(t), final_path(t), slimmer)
                slimmer.last_hour = hour_key(t)
                slimmer.save(state_path)
                msg += f", kept {kept:,}/{seen:,} events ({os.path.getsize(final_path(t)) / 1e6:.0f} MB)"
                if not keep_raw:
                    os.remove(raw_path(t))
            counts["ok"] += 1
            eta = (time.time() - t0) / i * (len(hours) - i)
            print(f"  [{i}/{len(hours)}] {hour_key(t)}  {msg}   eta {eta / 60:.0f} min", file=sys.stderr)
    print(json.dumps(counts), file=sys.stderr)
    return counts
