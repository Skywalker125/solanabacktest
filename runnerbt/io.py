"""Reading and writing event files (JSONL, JSONL.gz, JSON arrays)."""

from __future__ import annotations

import gzip
import io
import json
import os
from typing import Iterable, Iterator

EVENT_SUFFIXES = (".jsonl", ".jsonl.gz", ".ndjson", ".ndjson.gz", ".json", ".json.gz")


def _open_text(path: str, mode: str = "rt"):
    if path.endswith(".gz"):
        return gzip.open(path, mode, encoding="utf-8")
    return open(path, mode, encoding="utf-8")


def list_event_files(paths: Iterable[str]) -> list[str]:
    """Expand files/directories into a sorted list of event files.

    Archives are hourly, so lexicographic order of zero-padded names
    (e.g. 2026-10-01/13.jsonl.gz) is chronological order.
    """
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in files:
                    if f.endswith(EVENT_SUFFIXES):
                        out.append(os.path.join(root, f))
        elif os.path.exists(p):
            out.append(p)
        else:
            raise FileNotFoundError(p)
    return sorted(out)


def iter_file(path: str) -> Iterator[dict]:
    with _open_text(path) as fh:
        first = fh.read(1)
        while first and first.isspace():
            first = fh.read(1)
        if not first:
            return
        if first == "[" or (first == "{" and path.endswith((".json", ".json.gz"))):
            data = json.loads(first + fh.read())
            if isinstance(data, dict):
                data = data.get("events") or data.get("data") or [data]
            yield from data
            return
        buf = io.StringIO()
        buf.write(first)
        buf.write(fh.readline())
        line = buf.getvalue().strip()
        if line:
            yield json.loads(line)
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def iter_events(paths: Iterable[str]) -> Iterator[dict]:
    for f in list_event_files(paths):
        yield from iter_file(f)


def write_jsonl(path: str, rows: Iterable[dict]) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    n = 0
    with _open_text(path, "wt") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")))
            fh.write("\n")
            n += 1
    return n


def read_jsonl(path: str) -> Iterator[dict]:
    yield from iter_file(path)
