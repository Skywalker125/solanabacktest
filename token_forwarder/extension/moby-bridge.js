// Isolated-world relay: page hook (moby-hook.js) -> extension background.
window.addEventListener("message", (event) => {
  if (event.source !== window || !event.data?.__tokenForwarder) return;
  const { kind, value, entries } = event.data;
  if (kind === "auth") chrome.runtime.sendMessage({ type: "moby-auth", value });
  if (kind === "entries") chrome.runtime.sendMessage({ type: "moby-entries", entries });
});
