"""Receive token addresses from the Chrome extension and post them to Telegram.

The extension POSTs {"source": "gmgn" | "moby", "address": "<mint>"} to /token.
Each source maps to its own channel; every (source, address) pair is posted once,
and the sent set survives restarts (sent_tokens.json).
"""
import asyncio
import json
import logging
import os
import re
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = str(HERE / os.environ.get("TG_SESSION", "forwarder"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")

CHANNELS = {
    "gmgn": os.environ["GMGN_CHANNEL"],
    "moby": os.environ["MOBY_CHANNEL"],
}

SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SENT_FILE = HERE / "sent_tokens.json"

log = logging.getLogger("forwarder")


def _channel_target(value: str):
    """Numeric ids (e.g. -1001234567890) must be ints for Telethon."""
    return int(value) if re.fullmatch(r"-?\d+", value) else value


class Forwarder:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.entities = {}
        self.sent = self._load_sent()
        self.lock = asyncio.Lock()

    @staticmethod
    def _load_sent():
        if SENT_FILE.exists():
            data = json.loads(SENT_FILE.read_text())
            return {src: set(addrs) for src, addrs in data.items()}
        return {src: set() for src in CHANNELS}

    def _save_sent(self):
        SENT_FILE.write_text(json.dumps({s: sorted(a) for s, a in self.sent.items()}))

    async def resolve_channels(self):
        for source, target in CHANNELS.items():
            self.entities[source] = await self.client.get_entity(_channel_target(target))
            log.info("%s -> %s", source, target)

    async def post(self, source: str, address: str) -> bool:
        async with self.lock:
            seen = self.sent.setdefault(source, set())
            if address in seen:
                return False
            await self.client.send_message(self.entities[source], address)
            seen.add(address)
            self._save_sent()
            log.info("[%s] posted %s", source, address)
            return True


@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Secret"
    return resp


async def handle_token(request: web.Request):
    if SHARED_SECRET and request.headers.get("X-Secret") != SHARED_SECRET:
        return web.json_response({"ok": False, "error": "bad secret"}, status=401)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    source = str(body.get("source", "")).lower()
    address = str(body.get("address", "")).strip()
    if source not in CHANNELS:
        return web.json_response({"ok": False, "error": f"unknown source {source!r}"}, status=400)
    if not SOLANA_ADDRESS.match(address):
        return web.json_response({"ok": False, "error": "not a solana address"}, status=400)

    forwarder: Forwarder = request.app["forwarder"]
    try:
        posted = await forwarder.post(source, address)
    except Exception as exc:  # flood wait, permissions, network...
        log.exception("[%s] failed to post %s", source, address)
        return web.json_response({"ok": False, "error": str(exc)}, status=502)
    return web.json_response({"ok": True, "posted": posted})


async def handle_health(request: web.Request):
    return web.json_response({"ok": True, "channels": list(CHANNELS)})


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()  # asks for phone + code on the first run
    forwarder = Forwarder(client)
    await forwarder.resolve_channels()

    app = web.Application(middlewares=[cors])
    app["forwarder"] = forwarder
    app.router.add_post("/token", handle_token)
    app.router.add_get("/health", handle_health)
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    log.info("listening on http://%s:%d", HOST, PORT)
    try:
        await client.run_until_disconnected()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
