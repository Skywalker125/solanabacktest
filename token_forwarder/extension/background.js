// 1. Relays token addresses from content scripts (GMGN) to the Python backend.
// 2. Polls the Moby screener API directly. The bearer token is taken from the
//    requests the app.moby.win page makes, so keep a Moby tab open (logged in).
// Requests go from here (not the page) so the sites' CSP/CORS never get in the way.

const DEFAULTS = {
  backendUrl: "http://127.0.0.1:8765",
  secret: "",
  mobyPollSeconds: 5,
  mobyMaxAgeMinutes: 60,
};
const MOBY_API_HOST = "https://web-api.mobyscreener.com/";
const MOBY_URL =
  "https://web-api.mobyscreener.com/web/api_v2/tokens/screener/leaderboard/?network=solana&networks=solana";

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}

async function log(entry) {
  const { history = [] } = await chrome.storage.local.get("history");
  history.unshift({ time: Date.now(), ...entry });
  await chrome.storage.local.set({ history: history.slice(0, 50) });
}

async function forward(source, address, note) {
  const { backendUrl, secret } = await settings();
  try {
    const resp = await fetch(`${backendUrl.replace(/\/+$/, "")}/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Secret": secret },
      body: JSON.stringify({ source, address }),
    });
    const data = await resp.json().catch(() => ({}));
    const ok = resp.ok && data.ok;
    await log({ source, address, note, ok, posted: !!data.posted, error: ok ? null : data.error || `HTTP ${resp.status}` });
    return { ok };
  } catch (err) {
    await log({ source, address, note, ok: false, error: String(err) });
    return { ok: false };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "token") {
    forward(msg.source, msg.address).then(sendResponse);
    return true; // async response
  }
});

// ---------------------------------------------------------------- Moby ----

// Grab the bearer token from the page's own API calls.
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    const auth = details.requestHeaders?.find((h) => h.name.toLowerCase() === "authorization");
    if (auth?.value?.startsWith("Bearer ")) {
      chrome.storage.local.get("mobyAuth").then(({ mobyAuth }) => {
        if (mobyAuth !== auth.value) {
          chrome.storage.local.set({ mobyAuth: auth.value, mobyAuthAt: Date.now(), mobyStatus: "token captured" });
        }
      });
    }
  },
  { urls: [MOBY_API_HOST + "*"] },
  ["requestHeaders", "extraHeaders"]
);

const SOLANA_RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

// "2026-10-05T08:27:44" has no zone; the API returns UTC.
function createdMs(s) {
  if (!s) return NaN;
  return Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z");
}

// Seen set lives in session storage: survives service-worker restarts, but is
// cleared when the browser starts, so each browser start begins with a silent baseline.
async function loadSeen() {
  const { mobySeen } = await chrome.storage.session.get("mobySeen");
  return mobySeen ? new Set(mobySeen) : null;
}
async function saveSeen(set) {
  await chrome.storage.session.set({ mobySeen: [...set].slice(-5000) });
}

let polling = false;

async function pollMoby() {
  if (polling) return;
  polling = true;
  try {
    const s = await chrome.storage.local.get(["enabled_moby", "mobyAuth"]);
    if (s.enabled_moby === false) return;
    if (!s.mobyAuth) {
      await chrome.storage.local.set({ mobyStatus: "no token yet - open app.moby.win" });
      return;
    }

    const resp = await fetch(MOBY_URL, {
      headers: { accept: "application/json", authorization: s.mobyAuth },
      cache: "no-store",
    });
    if (resp.status === 401 || resp.status === 403) {
      await chrome.storage.local.set({ mobyStatus: `HTTP ${resp.status} - token expired, reload app.moby.win` });
      return;
    }
    if (!resp.ok) {
      await chrome.storage.local.set({ mobyStatus: `HTTP ${resp.status}` });
      return;
    }
    const entries = (await resp.json())?.entries || [];
    const tokens = entries.filter((e) => e.network === "solana" && SOLANA_RE.test(e.token_address || ""));

    let seen = await loadSeen();
    if (!seen) {
      // Startup: remember everything listed now, post nothing.
      seen = new Set(tokens.map((e) => e.token_address));
      await saveSeen(seen);
      await chrome.storage.local.set({ mobyStatus: `baseline: ${seen.size} tokens (${new Date().toLocaleTimeString()})` });
      return;
    }

    const { mobyMaxAgeMinutes } = await settings();
    const maxAgeMs = Number(mobyMaxAgeMinutes) * 60_000;
    let changed = false;
    for (const e of tokens) {
      const addr = e.token_address;
      if (seen.has(addr)) continue;
      const age = Date.now() - createdMs(e.token_created);
      if (!(age <= maxAgeMs)) {
        seen.add(addr); // new on the list but too old (or no date): never post it
        changed = true;
        continue;
      }
      const minutes = Math.max(0, Math.round(age / 60_000));
      const { ok } = await forward("moby", addr, `${e.token_symbol || "?"}, ${minutes}m old`);
      if (ok) {
        seen.add(addr); // failed sends stay unseen and retry next poll
        changed = true;
      }
    }
    if (changed) await saveSeen(seen);
    await chrome.storage.local.set({ mobyStatus: `ok: ${tokens.length} tokens (${new Date().toLocaleTimeString()})` });
  } catch (err) {
    await chrome.storage.local.set({ mobyStatus: `error: ${err}` });
  } finally {
    polling = false;
  }
}

// setTimeout loop for the short interval; the alarm restarts it if Chrome
// suspended the service worker.
let loopTimer = null;
async function loop() {
  clearTimeout(loopTimer);
  await pollMoby();
  const { mobyPollSeconds } = await settings();
  loopTimer = setTimeout(loop, Math.max(2, Number(mobyPollSeconds) || 5) * 1000);
}

chrome.alarms.create("moby-watchdog", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "moby-watchdog" && loopTimer === null) loop();
});
chrome.runtime.onStartup.addListener(loop);
chrome.runtime.onInstalled.addListener(loop);
loop();
