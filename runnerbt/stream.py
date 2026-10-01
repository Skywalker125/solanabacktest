"""One shared Socket.IO connection to the Advanced Data Stream.

`stream` messages may carry one event or a list of events; both are unpacked
here so callers always get single event dicts.  Connection problems, server
errors and unexpected event names are printed instead of failing silently.
"""

from __future__ import annotations

import sys
import time
import traceback
from typing import Callable, Optional

STREAM_URL = "https://sol.shrine.trade"


def _log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def unpack(data) -> list:
    """Normalise a `stream` payload into a list of event dicts."""
    if isinstance(data, dict):
        # some servers wrap batches: {"events": [...]} / {"data": [...]}
        inner = data.get("events") if "action" not in data else None
        if inner is None and "action" not in data:
            inner = data.get("data")
        return [e for e in inner if isinstance(e, dict)] if isinstance(inner, list) else [data]
    if isinstance(data, list):
        out = []
        for x in data:
            out.extend(unpack(x))
        return out
    if isinstance(data, (str, bytes)):
        import json
        try:
            return unpack(json.loads(data))
        except ValueError:
            return []
    return []


class StreamStats:
    def __init__(self):
        self.messages = 0
        self.events = 0
        self.errors = 0
        self.other_events: dict = {}
        self.connected_at: Optional[float] = None


def run_stream(on_event: Callable[[dict], None], protocols=None, actions=None, url: str = STREAM_URL,
               on_tick: Optional[Callable[[], None]] = None, tick_s: float = 1.0,
               status_every_s: float = 30.0, stats: Optional[StreamStats] = None, debug: bool = False):
    """Connect, subscribe, and call `on_event(ev)` for every event until Ctrl+C."""
    import socketio  # python-socketio[client]

    st = stats or StreamStats()
    sio = socketio.Client(reconnection=True, reconnection_delay=1, reconnection_delay_max=30,
                          logger=debug, engineio_logger=debug)
    payload = {}
    if protocols:
        payload["protocols"] = list(protocols)
    if actions:
        payload["actions"] = list(actions)

    def on_ack(*resp):
        _log(f"subscribe_stream acknowledged: {resp!r}"[:300])

    @sio.event
    def connect():
        st.connected_at = time.time()
        _log(f"connected to {url} (sid {sio.sid}); subscribing {payload or 'to everything'}")
        # subscriptions are per socket: re-subscribe on every (re)connect
        sio.emit("subscribe_stream", payload, callback=on_ack)

    @sio.event
    def connect_error(data):
        _log(f"connection refused: {data!r}")

    @sio.event
    def disconnect(*reason):
        _log(f"disconnected {reason or ''} - reconnecting")

    @sio.on("stream")
    def on_stream(data):
        st.messages += 1
        if st.messages == 1:
            kind = f"batch of {len(data)}" if isinstance(data, list) else type(data).__name__
            _log(f"first stream message received ({kind})")
        for ev in unpack(data):
            st.events += 1
            try:
                on_event(ev)
            except Exception:
                st.errors += 1
                if st.errors <= 5:
                    _log("event handler failed:\n" + traceback.format_exc())

    @sio.on("*")
    def any_event(event, *args):
        # errors/notices from the server (rate limits, bad subscription, ...) land here
        n = st.other_events.get(event, 0) + 1
        st.other_events[event] = n
        if n <= 3:
            _log(f"server sent '{event}': {args!r}"[:500])

    import signal

    def _term(*_):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _term)  # stop cleanly (files closed) on kill/timeout
    except (ValueError, AttributeError):
        pass  # not in the main thread
    _log(f"connecting to {url} ...")
    delay = 2.0
    while True:  # the client only auto-reconnects after a first successful connect
        try:
            sio.connect(url, transports=["websocket", "polling"], wait_timeout=20)
            break
        except KeyboardInterrupt:
            return st
        except Exception as e:
            _log(f"connect failed ({e or type(e).__name__}); retrying in {delay:.0f}s")
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                return st
            delay = min(delay * 2, 60)
    last_status = time.time()
    try:
        while True:
            sio.sleep(tick_s)
            if on_tick:
                on_tick()
            if time.time() - last_status >= status_every_s:
                last_status = time.time()
                extra = f", other events {st.other_events}" if st.other_events else ""
                _log(f"{st.events:,} events in {st.messages:,} messages, "
                     f"{st.errors} handler errors, connected={sio.connected}{extra}")
                if st.messages == 0 and st.connected_at and time.time() - st.connected_at > 60:
                    _log("no 'stream' messages after 60s: the subscription was not accepted or the "
                         "endpoint changed. Run with --debug to see the raw socket traffic.")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if sio.connected:
                sio.emit("unsubscribe_stream")
        finally:
            sio.disconnect()
    return st
