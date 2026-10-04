# Token Forwarder (GMGN + Moby → Telegram)

A Chrome extension watches two pages for new Solana token addresses. A local Python backend
then posts each address from your own Telegram account (Telethon), with a separate channel for each page.

```
gmgn.ai/follow ─┐  content script     background.js   POST /token        Telethon
                ├────────────────────►───────────────►  backend/server.py ──► GMGN_CHANNEL
app.moby.win ───┘  {source, address}                    (dedupe per page) ──► MOBY_CHANNEL
```

| Page | What is detected | Refresh |
|---|---|---|
| `https://gmgn.ai/follow?popout=true&target=wallet&chain=sol` | every `a[href="/sol/token/<mint>"]` inside `div.flex.items-center.overflow-hidden` that has not been seen before | page reloads every **5 s** |
| `https://app.moby.win/` | the **first row's** logo `<img class="rounded-full object-cover">`. The mint is pulled from `src` with a regex (`/<networkId>_<mint>_` on token-media.defined.fi, with a generic base58 fallback). Network ids other than Solana (`1399811149`) are skipped | none (MutationObserver + 1 s safety check) |

On the first run, each page only records the tokens it already shows and sends nothing, so you
don't flood the channel. Seen addresses are stored in the extension. The backend also keeps
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
