const $ = (id) => document.getElementById(id);
const status = (t) => { $("status").textContent = t; };

async function load() {
  const s = await chrome.storage.local.get(["backendUrl", "secret", "enabled_gmgn", "enabled_moby",
    "mobyPollSeconds", "mobyMaxAgeMinutes"]);
  $("backendUrl").value = s.backendUrl || "http://127.0.0.1:8765";
  $("secret").value = s.secret || "";
  $("enabled_gmgn").checked = s.enabled_gmgn !== false;
  $("enabled_moby").checked = s.enabled_moby !== false;
  $("mobyPollSeconds").value = s.mobyPollSeconds || 5;
  $("mobyMaxAgeMinutes").value = s.mobyMaxAgeMinutes || 60;
}

// Live parts only, so typing in the inputs is never overwritten.
async function refresh() {
  const s = await chrome.storage.local.get(["history", "mobyStatus", "mobyAuthAt"]);
  const tok = s.mobyAuthAt ? `token from ${new Date(s.mobyAuthAt).toLocaleTimeString()}` : "no token yet";
  $("mobyStatus").textContent = `Moby: ${s.mobyStatus || "starting"} · ${tok}`;
  $("history").replaceChildren(...(s.history || []).map((h) => {
    const li = document.createElement("li");
    li.className = h.ok ? "ok" : "err";
    const when = new Date(h.time).toLocaleTimeString();
    const what = (h.note ? `(${h.note}) ` : "") + (h.ok ? (h.posted ? "posted" : "dup") : `error: ${h.error}`);
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
    mobyPollSeconds: Math.max(2, Number($("mobyPollSeconds").value) || 5),
    mobyMaxAgeMinutes: Math.max(1, Number($("mobyMaxAgeMinutes").value) || 60),
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
  await chrome.storage.local.remove(["seen_gmgn"]);
  await chrome.storage.session.remove("mobySeen"); // next Moby poll re-baselines silently
  status("seen lists cleared");
};

load();
refresh();
setInterval(refresh, 2000);
