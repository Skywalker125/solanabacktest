// GMGN follow page: every token link (<a href="/sol/token/<mint>">) in the feed under
// #GlobalScrollDomId is checked; unseen addresses go to the gmgn channel.
// The page reloads every 5 s.

(async () => {
  const SOURCE = "gmgn";
  const RELOAD_MS = 5000;
  const TOKEN_HREF_RE = /\/sol\/token\/([1-9A-HJ-NP-Za-km-z]{32,44})/;
  const log = (...a) => console.log("[token-forwarder:gmgn]", ...a);

  if (!location.pathname.includes("/follow")) return;
  if (!(await TF.isEnabled(SOURCE))) { log("disabled in popup"); return; }

  const seen = await TF.loadSeen(SOURCE);
  const pending = new Set();
  const inflight = [];

  function collect() {
    const root = document.getElementById("GlobalScrollDomId") || document;
    const found = [];
    for (const a of root.querySelectorAll('a[href*="/sol/token/"]')) {
      const m = (a.getAttribute("href") || "").match(TOKEN_HREF_RE);
      if (m && !found.includes(m[1])) found.push(m[1]);
    }
    return found;
  }

  function scan() {
    // First run ever: only learn what is already listed, send nothing.
    if (!seen.initialized) return;
    for (const address of collect()) {
      if (seen.set.has(address) || pending.has(address)) continue;
      pending.add(address);
      log("new token", address);
      inflight.push(
        TF.send(SOURCE, address).then(async (ok) => {
          pending.delete(address);
          if (ok) {
            seen.set.add(address);
            await TF.saveSeen(SOURCE, seen.set);
          } else {
            log("send failed (backend down?), will retry", address);
          }
        })
      );
    }
  }

  const observer = new MutationObserver(scan);
  observer.observe(document.body, { childList: true, subtree: true });
  scan();

  setTimeout(async () => {
    observer.disconnect();
    if (!seen.initialized) {
      const current = collect();
      // Only baseline once the list has rendered, otherwise the next load would flood.
      if (current.length) {
        current.forEach((a) => seen.set.add(a));
        await TF.saveSeen(SOURCE, seen.set);
        log(`first run: learned ${current.length} existing tokens, new ones are sent from now on`);
      } else {
        log("no token links found yet, will retry after reload");
      }
    } else {
      scan();
      log(`${collect().length} token links on page`);
    }
    await Promise.allSettled(inflight); // don't cut off a send mid-flight
    if (await TF.isEnabled(SOURCE)) location.reload();
  }, RELOAD_MS);
})();
