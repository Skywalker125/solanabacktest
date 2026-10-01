"""Download the hourly stream archive ("Historical replay") for a date range.

The archive URL is a template, so it works with whatever layout the provider
uses.  Placeholders (all UTC, for the start of each hour):

    {date}  2026-09-30     {yyyy} {mm} {dd} {HH}   zero padded
    {hour}  7 (no padding) {unix} 1790000000       {unix_ms} 1790000000000
    {iso}   2026-09-30T07:00:00Z

Files land in <out>/YYYY-MM-DD/HH.<ext>, the layout `build` reads.  Existing
files are skipped, so an interrupted download can simply be re-run.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone


def parse_when(s: str) -> datetime:
    s = s.strip().replace(" ", "T").rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H", "%Y-%m-%d"):
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


def render(template: str, t: datetime) -> str:
    unix = int(t.timestamp())
    return template.format(date=t.strftime("%Y-%m-%d"), yyyy=t.strftime("%Y"), mm=t.strftime("%m"),
                           dd=t.strftime("%d"), HH=t.strftime("%H"), hour=t.hour, unix=unix,
                           unix_ms=unix * 1000, iso=t.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _existing(out_dir: str, t: datetime):
    d = os.path.join(out_dir, t.strftime("%Y-%m-%d"))
    if not os.path.isdir(d):
        return None
    stem = t.strftime("%H") + "."
    for f in os.listdir(d):
        if f.startswith(stem) and not f.endswith(".part"):
            return os.path.join(d, f)
    return None


def sniff_ext(body: bytes) -> str:
    """Pick a file extension the loader understands from the payload itself."""
    gz = body[:2] == b"\x1f\x8b"
    text = gzip.decompress(body) if gz else body
    head = text[:4096].lstrip()
    suffix = ".gz" if gz else ""
    if head[:1] == b"[":
        return ".json" + suffix
    if head[:1] == b"{":
        first = text.lstrip().split(b"\n", 1)[0]
        try:
            obj = json.loads(first)
            return (".jsonl" if "action" in obj or "signature" in obj else ".json") + suffix
        except ValueError:
            return ".json" + suffix
    raise ValueError(f"unrecognised archive payload (starts with {body[:16]!r}); "
                     "if it is zstd/parquet, tell me the format and I'll add a reader")


def fetch_hour(template: str, t: datetime, out_dir: str, headers: dict, retries: int = 4,
               timeout: float = 120.0) -> tuple[str, str]:
    """Returns (status, detail): status is ok / skip / missing / error."""
    have = _existing(out_dir, t)
    if have:
        return "skip", have
    url = render(template, t)
    req = urllib.request.Request(url, headers={"User-Agent": "runnerbt/0.1", **headers})
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" and body[:2] == b"\x1f\x8b":
                pass  # urllib does not auto-decompress; keep the gzip as is
            ext = sniff_ext(body)
            d = os.path.join(out_dir, t.strftime("%Y-%m-%d"))
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, t.strftime("%H") + ext)
            with open(path + ".part", "wb") as fh:
                fh.write(body)
            os.replace(path + ".part", path)
            return "ok", f"{path} ({len(body) / 1e6:.1f} MB)"
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return "missing", f"{url} -> HTTP {e.code}"
            if e.code in (401, 403):
                return "error", f"{url} -> HTTP {e.code} (needs an API key? pass --header)"
            err = f"HTTP {e.code}"
            if e.code == 429:
                delay = max(delay, float(e.headers.get("Retry-After") or 10))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            err = str(e)
        except ValueError as e:
            return "error", f"{url}: {e}"
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    return "error", f"{url}: {err}"


def fetch_range(template: str, start: datetime, end: datetime, out_dir: str, headers: dict | None = None,
                workers: int = 4) -> dict:
    hours = list(hours_between(start, end))
    counts = {"ok": 0, "skip": 0, "missing": 0, "error": 0}
    print(f"fetching {len(hours)} hourly files {hours[0]:%Y-%m-%d %H}:00 .. {hours[-1]:%Y-%m-%d %H}:00 UTC "
          f"-> {os.path.abspath(out_dir)}", file=sys.stderr)
    print(f"first url: {render(template, hours[0])}", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(fetch_hour, template, t, out_dir, headers or {}): t for t in hours}
        for f in as_completed(futs):
            status, detail = f.result()
            counts[status] += 1
            if status != "skip":
                print(f"  {status:<7} {futs[f]:%Y-%m-%d %H}h  {detail}", file=sys.stderr)
            if counts["error"] >= 5 and counts["ok"] == 0:
                print("5 errors and no successful download: check the URL template", file=sys.stderr)
                pool.shutdown(cancel_futures=True)
                break
    print(json.dumps(counts), file=sys.stderr)
    return counts
