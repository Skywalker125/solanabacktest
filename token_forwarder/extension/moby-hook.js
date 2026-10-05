// Runs inside the app.moby.win page (MAIN world). Wraps fetch/XHR so we see
// what the page itself sends and receives from web-api.mobyscreener.com:
//  - the "Authorization: Bearer ..." header  -> extension polls with it
//  - leaderboard JSON responses             -> processed directly
// Data goes to moby-bridge.js via window.postMessage.
(() => {
  const API = "mobyscreener.com";
  const LEADERBOARD = "/tokens/screener/leaderboard";
  const post = (payload) => window.postMessage({ __tokenForwarder: true, ...payload }, location.origin);

  let lastAuth = null;
  function seenAuth(value) {
    if (typeof value === "string" && /^Bearer\s+\S+/i.test(value) && value !== lastAuth) {
      lastAuth = value;
      post({ kind: "auth", value });
    }
  }
  function seenEntries(json) {
    if (json && Array.isArray(json.entries)) post({ kind: "entries", entries: json.entries });
  }

  function authFrom(headers) {
    if (!headers) return null;
    if (headers instanceof Headers) return headers.get("authorization");
    if (Array.isArray(headers)) {
      const h = headers.find(([k]) => String(k).toLowerCase() === "authorization");
      return h ? h[1] : null;
    }
    for (const k of Object.keys(headers)) if (k.toLowerCase() === "authorization") return headers[k];
    return null;
  }

  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input?.url || String(input);
    if (url.includes(API)) {
      try {
        seenAuth(authFrom(init?.headers) || (input instanceof Request ? input.headers.get("authorization") : null));
      } catch (_) {}
    }
    const resp = await origFetch.apply(this, arguments);
    if (url.includes(API) && url.includes(LEADERBOARD) && resp.ok) {
      resp.clone().json().then(seenEntries).catch(() => {});
    }
    return resp;
  };

  const XHR = XMLHttpRequest.prototype;
  const origOpen = XHR.open, origSetHeader = XHR.setRequestHeader, origSend = XHR.send;
  XHR.open = function (method, url) {
    this.__tfUrl = String(url);
    return origOpen.apply(this, arguments);
  };
  XHR.setRequestHeader = function (name, value) {
    if (this.__tfUrl?.includes(API) && String(name).toLowerCase() === "authorization") seenAuth(value);
    return origSetHeader.apply(this, arguments);
  };
  XHR.send = function () {
    if (this.__tfUrl?.includes(API) && this.__tfUrl.includes(LEADERBOARD)) {
      this.addEventListener("load", () => {
        if (this.status !== 200) return;
        try {
          seenEntries(this.responseType === "json" ? this.response : JSON.parse(this.responseText));
        } catch (_) {}
      });
    }
    return origSend.apply(this, arguments);
  };
})();
