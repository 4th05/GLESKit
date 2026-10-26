import "./web.css";
import { renderControls, bindControls } from "./web-controls.js";
import { createSettingsDialog } from "./settings-dialog.js";

const fragment = new URLSearchParams(window.location.hash.slice(1));
const launchNonce = fragment.get("launch");
let bootstrapNeeded = Boolean(launchNonce);
if (launchNonce) window.history.replaceState(null, "", window.location.pathname);
document.body.classList.add("local-web");
document.title = "GLESKit · Local workspace";

let tab = window.sessionStorage.getItem("gleskit-web-tab");
if (!tab || !/^[a-zA-Z0-9_-]{1,160}$/.test(tab)) {
  tab = crypto.randomUUID();
  window.sessionStorage.setItem("gleskit-web-tab", tab);
}
let csrf = "", revision = 0, source = null, readyPromise = null;
let writeQueue = Promise.resolve();
const listeners = new Set(), backlog = [], mediaUrls = new Map();
const panel = { status: null, settings: null, providers: [], catalog: [], loading: false, error: "", message: "", writable: false, connected: false };
let settingsDialog = null, chatSettingsChanged = false, settingsStateChanged = false;
const readCommands = new Set([
  "bootstrap_app", "bootstrap_workspace", "reference_status", "list_sessions", "get_session_snapshot", "get_session_summary",
  "list_cohorts", "list_cohort_providers", "cohort_catalog", "get_cohort", "get_artifact", "get_upload", "list_session_artifacts",
  "list_session_uploads", "list_session_notes", "get_session_note", "list_session_reports", "get_session_report", "get_chat_conversation",
  "list_chat_providers", "get_provider_catalog", "fetch_provider_models", "get_cohort_download_job", "get_web_status", "get_web_settings",
]);

async function request(url, init = {}) {
  let response;
  try {
    response = await fetch(url, { credentials: "same-origin", cache: "no-store", ...init });
  } catch {
    throw new Error("The local GLESKit server is unreachable. No action was automatically repeated. Restart the launcher if needed.");
  }
  const next = Number(response.headers.get("x-gleskit-revision"));
  if (Number.isFinite(next)) revision = Math.max(revision, next);
  let value;
  try { value = await response.json(); } catch { throw new Error("The local server returned an invalid response."); }
  if (!response.ok || value.ok === false) throw new Error(value.error?.message || "The local operation failed.");
  return value;
}

function headers() {
  return { "Content-Type": "application/json", "X-GLESKit-CSRF": csrf, "X-GLESKit-Tab": tab,
    "X-GLESKit-Revision": String(revision), "X-GLESKit-Request": crypto.randomUUID() };
}

async function ready() {
  if (!readyPromise) readyPromise = (async () => {
    if (bootstrapNeeded) {
      await request("/api/auth/bootstrap", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ nonce: launchNonce }) });
      bootstrapNeeded = false;
    }
    const session = await request("/api/session");
    csrf = session.csrf;
    revision = session.runtime.revision;
    const control = await request("/api/control", { method: "POST", headers: headers(), body: JSON.stringify({ tab }) });
    panel.writable = control.writable;
    revision = control.revision;
    panel.connected = true;
    publishRuntime(control.runtime || session.runtime);
    source = new EventSource("/api/events?tab=" + encodeURIComponent(tab));
    let opened = false, recovering = null;
    source.addEventListener("error", () => {
      panel.connected = false; panel.writable = false; updatePanel();
    });
    source.addEventListener("open", () => {
      if (!opened) { opened = true; return; }
      if (recovering) return;
      recovering = (async () => {
        const session = await request("/api/session");
        csrf = session.csrf; revision = session.runtime.revision;
        const control = await request("/api/control", { method: "POST", headers: headers(), body: JSON.stringify({ tab }) });
        panel.connected = true; panel.writable = control.writable; revision = control.revision;
        panel.error = "";
        publishRuntime(control.runtime || session.runtime);
        document.dispatchEvent(new CustomEvent("gleskit:chat-reconcile"));
        updatePanel();
      })().catch(() => {
        panel.error = "The local connection expired or the server restarted. Run make web_app to reopen an authenticated browser session.";
        updatePanel();
      }).finally(() => { recovering = null; });
    });
    source.addEventListener("gleskit-runtime", (event) => {
      try {
        const value = JSON.parse(event.data);
        panel.writable = value.writable; revision = Math.max(revision, value.runtime.revision);
        publishRuntime(value.runtime);
      } catch { /* Ignore malformed notifications; never repeat a mutation. */ }
    });
    source.addEventListener("gleskit-chat", (event) => {
      let value;
      try { value = JSON.parse(event.data); } catch { return; }
      if (listeners.size) for (const callback of listeners) callback({ payload: value });
      else { backlog.push(value); if (backlog.length > 512) backlog.shift(); }
    });
    source.addEventListener("gleskit-reconcile", () => {
      document.dispatchEvent(new CustomEvent("gleskit:chat-reconcile"));
      for (const callback of listeners) callback({ payload: { event_id: crypto.randomUUID(), event_type: "system.warning",
        conversation_id: "", turn_id: "", payload: { message: "Connection restored. Reopen the selected conversation to reconcile any missed activity." } } });
    });
  })().catch((error) => { readyPromise = null; throw error; });
  return readyPromise;
}

function collectMedia(value) {
  const urls = value?.payload?.web_file_urls;
  if (urls) for (const [path, url] of Object.entries(urls)) mediaUrls.set(path, url);
}

export function convertFileSrc(path) { return mediaUrls.get(path) || ""; }

export async function invoke(command, values = {}) {
  if (command === "open_external_url") {
    const url = new URL(values.url);
    if (!["http:", "https:"].includes(url.protocol)) throw new Error("Only HTTP(S) links can be opened.");
    window.open(url.href, "_blank", "noopener,noreferrer");
    return;
  }
  await ready();
  if (command === "write_runtime_heartbeat" && !panel.writable) return (await request("/api/session")).runtime;
  const run = async () => {
    const result = await request("/api/commands/" + command, { method: "POST", headers: headers(), body: JSON.stringify(values) });
    collectMedia(result.result);
    return result.result;
  };
  if (readCommands.has(command)) return run();
  const result = writeQueue.then(run, run);
  writeQueue = result.catch(() => {});
  return result;
}

export async function listen(name, callback) {
  if (name !== "gleskit://chat-turn-event") throw new Error("Unknown local event channel.");
  await ready();
  listeners.add(callback);
  for (const event of backlog.splice(0)) callback({ payload: event });
  return () => listeners.delete(callback);
}

export async function stageFile(file) {
  await ready();
  if (!panel.writable) throw new Error("Take control in Settings before uploading a file.");
  if (file.size > 1024 ** 3) throw new Error("Copied uploads are limited to 1 GiB. Link the local file instead.");
  return (await request("/api/uploads/stage", { method: "POST", headers: { ...headers(), "Content-Type": "application/octet-stream" }, body: file })).staging_id;
}

function updatePanel() {
  const notice = (field, severity) => {
    const message = panel[field];
    return { source: field, severity, message, acknowledge() {
      if (panel[field] === message) panel[field] = "";
      updatePanel();
    } };
  };
  document.dispatchEvent(new CustomEvent("gleskit:notices", { detail: { group: "settings", entries: [
    notice("error", "error"),
    notice("message", "success"),
    { source: "engine", severity: "error", message: panel.status?.engine?.error },
    { source: "mcp", severity: "error", message: panel.status?.mcp?.error },
    { source: "control", severity: "warning", message: panel.connected && !panel.writable ? "This browser tab is read-only. Use Take control in Settings before making changes." : "" },
  ] } }));
  document.dispatchEvent(new CustomEvent("gleskit:system-updated"));
  settingsDialog?.refresh();
}
function publishRuntime(runtime) { document.dispatchEvent(new CustomEvent("gleskit:runtime-updated", { detail: { runtime } })); }

export async function loadSystemPanel() {
  if (panel.loading) return;
  panel.loading = true; panel.error = ""; updatePanel();
  try {
    [panel.status, panel.settings, panel.providers, panel.catalog] = await Promise.all([
      invoke("get_web_status"), invoke("get_web_settings"), invoke("list_chat_providers"), invoke("get_provider_catalog"),
    ]);
  } catch (error) { panel.error = error.message; }
  finally {
    panel.loading = false; updatePanel();
    if (settingsDialog && !settingsDialog.isOpen && settingsStateChanged) publishClosedSettings();
  }
}

export function renderSystemPanel() { return renderControls(panel); }
function publishClosedSettings() {
  // A save/status refresh may still be in flight when the user closes Settings.
  // Publish the settled metadata in loadSystemPanel's finally, not a stale copy.
  if (panel.loading) return;
  if (panel.status) document.dispatchEvent(new CustomEvent("gleskit:settings-closed", { detail: {
    providers: panel.providers, mcp: panel.status.mcp, chatChanged: chatSettingsChanged,
  } }));
  chatSettingsChanged = false; settingsStateChanged = false;
}
export function openSystemPanel() {
  if (settingsDialog?.isOpen) return;
  settingsDialog ||= createSettingsDialog({ render: renderSystemPanel, bind: bindSystemPanel, onClose: publishClosedSettings });
  settingsDialog.open();
  void loadSystemPanel();
}
export function bindSystemPanel() {
  bindControls(panel, async (command, values) => {
    if (command === "take_control") {
      await ready();
      const control = await request("/api/control", { method: "POST", headers: headers(), body: JSON.stringify({ tab, takeover: true }) });
      panel.writable = control.writable; revision = control.revision;
      publishRuntime(control.runtime || {});
      return;
    }
    const result = await invoke(command, values);
    if (["save_chat_provider", "delete_chat_provider", "start_web_engine", "reconnect_chat_runtime"].includes(command)) chatSettingsChanged = true;
    if (command === "stop_web_engine") chatSettingsChanged = false; // Respect an intentional Stop on return to Chat.
    if (["save_chat_provider", "delete_chat_provider", "start_web_engine", "reconnect_chat_runtime", "stop_web_engine"].includes(command)) settingsStateChanged = true;
    return result;
  }, loadSystemPanel, updatePanel);
}
