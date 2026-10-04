const $ = (id) => document.getElementById(id);
const status = (t) => { $("status").textContent = t; };

async function load() {
  const s = await chrome.storage.local.get(["backendUrl", "secret", "enabled_gmgn", "enabled_moby", "history"]);
  $("backendUrl").value = s.backendUrl || "http://127.0.0.1:8765";
  $("secret").value = s.secret || "";
  $("enabled_gmgn").checked = s.enabled_gmgn !== false;
  $("enabled_moby").checked = s.enabled_moby !== false;
  $("history").replaceChildren(...(s.history || []).map((h) => {
    const li = document.createElement("li");
    li.className = h.ok ? "ok" : "err";
    const when = new Date(h.time).toLocaleTimeString();
    const what = h.ok ? (h.posted ? "posted" : "dup") : `error: ${h.error}`;
    li.textContent = `${when} [${h.source}] ${h.address} — ${what}`;
    return li;
  }));
}

$("save").onclick = async () => {
  await chrome.storage.local.set({
    backendUrl: $("backendUrl").value.trim(),
    secret: $("secret").value,
    enabled_gmgn: $("enabled_gmgn").checked,
    enabled_moby: $("enabled_moby").checked,
  });
  status("saved (reload the tabs)");
};

$("test").onclick = async () => {
  try {
    const r = await fetch(`${$("backendUrl").value.trim().replace(/\/+$/, "")}/health`);
    status(r.ok ? "backend OK" : `HTTP ${r.status}`);
  } catch (e) {
    status("backend unreachable");
  }
};

$("reset").onclick = async () => {
  await chrome.storage.local.remove(["seen_gmgn", "seen_moby"]);
  status("seen lists cleared");
};

load();
