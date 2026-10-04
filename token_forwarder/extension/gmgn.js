// GMGN follow page: every token link in a row's name cell
// (<div class="flex items-center overflow-hidden"> <a href="/sol/token/<mint>">)
// is checked; unseen addresses go to the gmgn channel. The page reloads every 5 s.

(async () => {
  const SOURCE = "gmgn";
  const RELOAD_MS = 5000;
  const TOKEN_HREF_RE = /\/sol\/token\/([1-9A-HJ-NP-Za-km-z]{32,44})/;

  if (!(await TF.isEnabled(SOURCE))) return;

  const seen = await TF.loadSeen(SOURCE);
  const pending = new Set();
  const inflight = [];

  function collect() {
    const found = [];
    const links = document.querySelectorAll(
      'div.flex.items-center.overflow-hidden a[href*="/sol/token/"]'
    );
    for (const a of links) {
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
      inflight.push(
        TF.send(SOURCE, address).then(async (ok) => {
          pending.delete(address);
          if (ok) {
            seen.set.add(address);
            await TF.saveSeen(SOURCE, seen.set);
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
      collect().forEach((a) => seen.set.add(a));
      await TF.saveSeen(SOURCE, seen.set);
    } else {
      scan();
    }
    await Promise.allSettled(inflight); // don't cut off a send mid-flight
    if (await TF.isEnabled(SOURCE)) location.reload();
  }, RELOAD_MS);
})();
