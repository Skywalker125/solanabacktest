// Moby: watches the first row of the token table. When a new token lands in the first
// row, the mint is pulled from its logo src with a regex and sent to the moby channel.
// Example src:
//   https://wsrv.nl/?w=64&h=64&default=1&url=https://token-media.defined.fi/
//     1399811149_GbdUYiGiRxhr146N8d5hemgcscU8YUW8oYbKJhuApump_small_668507fbce88.png&output=webp
// (1399811149 is defined.fi's Solana network id.) No reload: the list updates live.

(async () => {
  const SOURCE = "moby";
  const SOLANA_NETWORK_ID = "1399811149";
  // <networkId>_<mint>_ in defined.fi media urls
  const DEFINED_RE = /\/(\d+)_([1-9A-HJ-NP-Za-km-z]{32,44})_/;
  // fallback: any base58 run of mint length bounded by / _ . ? & = or end
  const GENERIC_RE = /(?:^|[\/_=])([1-9A-HJ-NP-Za-km-z]{32,44})(?=[_.?&\/]|$)/;

  if (!(await TF.isEnabled(SOURCE))) return;

  const seen = await TF.loadSeen(SOURCE);
  let lastFirst = null;
  let baselined = false;
  let busy = false;

  function addressFromSrc(src) {
    let url = src || "";
    try { url = decodeURIComponent(url); } catch (_) {}
    const d = url.match(DEFINED_RE);
    if (d) return d[1] === SOLANA_NETWORK_ID ? d[2] : null; // other chain -> skip
    const g = url.match(GENERIC_RE);
    return g ? g[1] : null;
  }

  function firstAddress() {
    // Rows are CSS grids; fall back to any avatar image if the layout changes.
    let imgs = document.querySelectorAll('[style*="grid-template-columns"] img.rounded-full.object-cover');
    if (!imgs.length) imgs = document.querySelectorAll("img.rounded-full.object-cover");
    for (const img of imgs) {
      const addr = addressFromSrc(img.getAttribute("src"));
      if (addr) return addr;
    }
    return null;
  }

  async function check() {
    if (busy) return;
    const addr = firstAddress();
    if (!addr) return;

    // The token already on top when the page opens is not "new".
    if (!baselined) {
      baselined = true;
      lastFirst = addr;
      seen.set.add(addr);
      await TF.saveSeen(SOURCE, seen.set);
      return;
    }
    if (addr === lastFirst && seen.set.has(addr)) return;
    lastFirst = addr;
    if (seen.set.has(addr)) return;

    busy = true;
    try {
      if (await TF.send(SOURCE, addr)) {
        seen.set.add(addr);
        await TF.saveSeen(SOURCE, seen.set);
      }
    } finally {
      busy = false;
    }
  }

  // Coalesce a burst of DOM mutations into one check (no rAF: it pauses in background tabs).
  let scheduled = false;
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    Promise.resolve().then(() => { scheduled = false; check(); });
  };

  new MutationObserver(schedule).observe(document.body, {
    childList: true, subtree: true, attributes: true, attributeFilter: ["src"],
  });
  setInterval(check, 1000); // safety net (also retries a failed send)
  check();
})();
