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
        self.last_message_at: Optional[float] = None
        self.reconnects = 0


def run_stream(on_event: Callable[[dict], None], protocols=None, actions=None, url: str = STREAM_URL,
               on_tick: Optional[Callable[[], None]] = None, tick_s: float = 1.0,
               status_every_s: float = 30.0, stats: Optional[StreamStats] = None, debug: bool = False,
               status_extra: Optional[Callable[[], str]] = None, stall_s: float = 60.0):
    """Connect, subscribe, and call `on_event(ev)` for every event until Ctrl+C."""
    import socketio  # python-socketio[client]

    import queue

    st = stats or StreamStats()
    inbox: "queue.Queue" = queue.Queue()

    def handle(data):
        for ev in unpack(data):
            st.events += 1
            try:
                on_event(ev)
            except Exception:
                st.errors += 1
                if st.errors <= 5:
                    _log("event handler failed:\n" + traceback.format_exc())

    def drain(seconds: float):
        """Process queued messages for up to `seconds` (returns early when idle)."""
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            try:
                data = inbox.get(timeout=left)
            except queue.Empty:
                return
            handle(data)
    # reconnection is handled below: the server allows one stream socket per IP and refuses a
    # new subscription while it still holds the old (dead) one, so a blind auto-reconnect can
    # end up "connected" but receiving nothing
    sio = socketio.Client(reconnection=False, logger=debug, engineio_logger=debug)
    need = {"reason": None, "wait": 0.0}
    refused = {"n": 0}

    def request_reconnect(reason: str, wait: float):
        if need["reason"] is None:
            need["reason"], need["wait"] = reason, wait
    payload = {}
    if protocols:
        payload["protocols"] = list(protocols)
    if actions:
        payload["actions"] = list(actions)

    def on_ack(*resp):
        _log(f"subscribe_stream acknowledged: {resp!r}"[:300])
        r = resp[0] if resp and isinstance(resp[0], dict) else {}
        if r.get("error"):
            refused["n"] += 1
            # the old connection usually times out server-side within ~20-60s
            wait = min(30.0 * refused["n"], 180.0)
            request_reconnect(f"subscription refused ({r.get('error')})", wait)
        else:
            refused["n"] = 0

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
        request_reconnect(f"disconnected {reason or ''}", 2.0)

    @sio.on("stream")
    def on_stream(data):
        st.messages += 1
        st.last_message_at = time.time()
        if st.messages == 1:
            kind = f"batch of {len(data)}" if isinstance(data, list) else type(data).__name__
            _log(f"first stream message received ({kind})")
        # only hand over: the socket thread must stay free to answer the server's pings, and
        # all processing happens in the main loop (one thread touches the engine)
        inbox.put(data)

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
    def connect_with_retry() -> bool:
        delay = 2.0
        while True:
            try:
                _log(f"connecting to {url} ...")
                sio.connect(url, transports=["websocket", "polling"], wait_timeout=20)
                return True
            except KeyboardInterrupt:
                return False
            except Exception as e:
                _log(f"connect failed ({e or type(e).__name__}); retrying in {delay:.0f}s")
                try:
                    time.sleep(delay)
                except KeyboardInterrupt:
                    return False
                delay = min(delay * 2, 60)

    if not connect_with_retry():
        return st
    last_status = time.time()
    try:
        while True:
            drain(tick_s)
            if on_tick:
                on_tick()
            now = time.time()
            # watchdog: connected but silent means the subscription is dead
            last = st.last_message_at or st.connected_at or now
            if need["reason"] is None and now - last > stall_s:
                request_reconnect(f"no events for {now - last:.0f}s", 2.0)
            if need["reason"] is not None:
                reason, wait = need["reason"], need["wait"]
                _log(f"{reason} - reconnecting in {wait:.0f}s")
                try:
                    sio.disconnect()
                except Exception:
                    pass
                end = time.time() + wait
                while time.time() < end:  # keep working through what already arrived
                    drain(min(1.0, max(0.0, end - time.time())))
                    if time.time() < end:
                        time.sleep(min(0.2, max(0.0, end - time.time())))
                    if on_tick:
                        on_tick()
                need["reason"] = None
                st.reconnects += 1
                st.last_message_at = None
                if not connect_with_retry():
                    break
                continue
            if now - last_status >= status_every_s:
                last_status = now
                extra = f", other events {st.other_events}" if st.other_events else ""
                if st.reconnects:
                    extra += f", {st.reconnects} reconnects"
                if status_extra:
                    extra += "; " + status_extra()
                backlog = inbox.qsize()
                if backlog > 100:
                    extra += f", {backlog:,} messages queued (processing is falling behind)"
                _log(f"{st.events:,} events in {st.messages:,} messages, "
                     f"{st.errors} handler errors, connected={sio.connected}{extra}")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if sio.connected:
                sio.emit("unsubscribe_stream")
        finally:
            sio.disconnect()
    return st
