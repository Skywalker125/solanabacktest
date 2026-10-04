// Relays token addresses from the content scripts to the Python backend.
// Requests go from here (not the page) so the sites' CSP/CORS never get in the way.

const DEFAULTS = { backendUrl: "http://127.0.0.1:8765", secret: "" };

async function settings() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}

async function log(entry) {
  const { history = [] } = await chrome.storage.local.get("history");
  history.unshift({ time: Date.now(), ...entry });
  await chrome.storage.local.set({ history: history.slice(0, 50) });
}

async function forward(source, address) {
  const { backendUrl, secret } = await settings();
  try {
    const resp = await fetch(`${backendUrl.replace(/\/+$/, "")}/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Secret": secret },
      body: JSON.stringify({ source, address }),
    });
    const data = await resp.json().catch(() => ({}));
    const ok = resp.ok && data.ok;
    await log({ source, address, ok, posted: !!data.posted, error: ok ? null : data.error || `HTTP ${resp.status}` });
    return { ok };
  } catch (err) {
    await log({ source, address, ok: false, error: String(err) });
    return { ok: false };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "token") {
    forward(msg.source, msg.address).then(sendResponse);
    return true; // async response
  }
});
