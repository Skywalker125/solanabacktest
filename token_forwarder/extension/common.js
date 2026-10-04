// Shared helpers for the content scripts.

const SOLANA_RE = /[1-9A-HJ-NP-Za-km-z]{32,44}/;
const SEEN_LIMIT = 5000;

const TF = {
  isSolanaAddress(s) {
    return typeof s === "string" && new RegExp(`^${SOLANA_RE.source}$`).test(s);
  },

  async isEnabled(source) {
    const key = `enabled_${source}`;
    const data = await chrome.storage.local.get(key);
    return data[key] !== false; // on by default
  },

  // Persisted "already handled" set per page, so reloads don't resend.
  async loadSeen(source) {
    const key = `seen_${source}`;
    const data = await chrome.storage.local.get(key);
    return { initialized: Array.isArray(data[key]), set: new Set(data[key] || []) };
  },

  async saveSeen(source, set) {
    const arr = [...set].slice(-SEEN_LIMIT);
    await chrome.storage.local.set({ [`seen_${source}`]: arr });
  },

  // Resolves true when the backend accepted it (posted now or already posted).
  async send(source, address) {
    try {
      const res = await chrome.runtime.sendMessage({ type: "token", source, address });
      return !!res?.ok;
    } catch (err) {
      console.warn("[token-forwarder] send failed", err);
      return false;
    }
  },
};
