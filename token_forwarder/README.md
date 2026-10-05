# Token Forwarder (GMGN + Moby → Telegram)

A Chrome extension watches two pages for new Solana token addresses. A local Python backend
then posts each address from your own Telegram account (Telethon), with a separate channel for each page.

```
gmgn.ai/follow ─┐  content script       background.js   POST /token        Telethon
                ├────────────────────►───────────────►  backend/server.py ──► GMGN_CHANNEL
Moby API poll ──┘  (background.js)                      (dedupe per page) ──► MOBY_CHANNEL
```

| Page | What is detected | Refresh |
|---|---|---|
| `https://gmgn.ai/follow?popout=true&target=wallet&chain=sol` | every `a[href="/sol/token/<mint>"]` in the feed (`#GlobalScrollDomId`) that has not been seen before | page reloads every **5 s** |
| Moby screener API (`web-api.mobyscreener.com/.../leaderboard`) | polled from the extension background every **5 s** (setting). The bearer token is captured from the open `app.moby.win` tab: a small script in the page (`moby-hook.js`) sees the page's own API calls, and leaderboard responses the page loads are checked too. A token is posted when it is **new on the list and `token_created` is at most 60 min ago** (setting) | no page refresh. Keep one logged-in Moby tab open so the token stays fresh |

On the first run, nothing is sent. GMGN records the tokens already on the page. Moby records the
list from its first poll after each browser start, so a restart never sends anything. Seen addresses are stored in the extension. The backend also keeps
`sent_tokens.json`, so each address is posted at most once per channel.

## Backend

```bash
cd token_forwarder/backend
pip install -r requirements.txt
cp .env.example .env        # fill TG_API_ID / TG_API_HASH (my.telegram.org), GMGN_CHANNEL, MOBY_CHANNEL
python server.py            # first run asks for phone number + login code, creates forwarder.session
```

Channels can be `@username`, a `t.me/...` link, or a numeric id (`-100…`). The account must be
allowed to post in them. `GET http://127.0.0.1:8765/health` checks that the server is running.

## Extension

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and pick `token_forwarder/extension`.
2. Click the extension icon to set the backend URL and the optional secret (must match `SHARED_SECRET`),
   switch each page on or off, test the backend, and see the last 50 forwards.
3. Open both pages and leave them open. Chrome can freeze or discard background tabs, so give each page
   its own window (GMGN's `popout=true` is already one) instead of a hidden tab.

**Reset seen** clears the extension's memory. The next page load then records the current list again.

Debugging: open DevTools on the page and filter the console for `token-forwarder`. You'll see what was
found, what was sent, and whether the backend could be reached.

Moby status (token captured, last poll, errors) is shown in the popup. If it says `HTTP 401`, reload
the app.moby.win tab and the new token is picked up automatically.
