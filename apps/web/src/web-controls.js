import { providerDraft, renderModelPicker, bindModelPicker } from "./model-picker.js";

const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const SETTINGS_SECTIONS = [
  { id: "providers", label: "Providers & models", description: "Connect a provider, choose models and manage API keys." },
  { id: "instructions", label: "Instructions", description: "Set the preferences supplied with your research requests." },
  { id: "engine", label: "OpenCode", description: "Start, stop or reconnect your local chat engine and tools." },
  { id: "workspace", label: "Workspace & environment", description: "Choose local storage and inspect your scientific runtime." },
];

export function renderControls(panel) {
  const status = panel.status, engine = status?.engine, mcp = status?.mcp;
  const busy = panel.loading ? "disabled" : "";
  // undefined means a fresh Settings visit; null is an explicit new connection.
  if (panel.editProvider === undefined && panel.providers.length) {
    panel.editProvider = (panel.providers.find((row) => row.enabled) || panel.providers[0]).provider_id;
  }
  const editing = panel.providers.find((row) => row.provider_id === panel.editProvider);
  const kind = editing?.kind || panel.providerKind || "openrouter";
  const catalog = panel.catalog?.length ? panel.catalog : [
    { kind: "openrouter", label: "OpenRouter", endpoint: "https://openrouter.ai/api/v1" },
    { kind: "openai", label: "OpenAI", endpoint: "https://api.openai.com/v1" },
    { kind: "anthropic", label: "Anthropic", endpoint: "https://api.anthropic.com/v1" },
    { kind: "openai-compatible", label: "Other OpenAI-compatible", endpoint: "" },
    { kind: "local", label: "Local OpenAI-compatible (no key required)", endpoint: "http://127.0.0.1:11434/v1" },
  ];
  const preset = catalog.find((row) => row.kind === kind);
  const draft = providerDraft(panel, editing, preset);
  const section = SETTINGS_SECTIONS.find((row) => row.id === panel.section) || SETTINGS_SECTIONS[0];
  panel.section = section.id;
  return `<div class="web-settings-shell" data-settings-section="${section.id}">
    <header class="web-settings-heading"><h1 id="web-settings-title">Settings</h1><button id="web-settings-close" type="button" class="action-button compact" aria-label="Close Settings">Close</button></header>
    <div class="web-settings-body"><nav class="web-settings-nav" aria-label="Settings categories">${SETTINGS_SECTIONS.map((row) => `<button type="button" data-web-section="${row.id}" ${row.id === section.id ? 'aria-current="page"' : ""}>${escape(row.label)}</button>`).join("")}</nav>
    <main class="web-system" id="web-settings-content" aria-labelledby="web-settings-section-title">
    <header class="web-page-heading"><div><h2 id="web-settings-section-title" tabindex="-1">${escape(section.label)}</h2><p>${escape(section.description)}</p></div></header>
    ${panel.loading ? `<p class="reference-copy" role="status">Reading local service settings. No model or reference download is being started.</p>` : ""}
    ${!panel.writable ? `<div class="web-button-row"><button class="action-button compact" data-web-action="take-control">Take control of this tab</button></div>` : ""}
    ${section.id === "engine" ? `
    <section class="web-settings-card"><div class="web-card-title"><div><h2>OpenCode engine</h2><p>Started only when you ask. Navigation never restarts it.</p></div><span class="status-chip ${engine?.running ? "positive" : ""}">${engine?.running ? "Running" : "Stopped"}</span></div>
      <div class="web-button-row"><button class="action-button primary compact" data-web-action="start" ${busy || (engine?.running ? "disabled" : "")}>Start engine</button>
      <button class="action-button compact" data-web-action="reconnect" ${busy}>Reconnect once</button>
      <button class="action-button compact danger" data-web-action="stop" ${busy || (!engine?.running ? "disabled" : "")}>Stop engine</button></div>
      <p class="reference-copy" role="status">GLESKit MCP: ${mcp?.running ? "Connected — tools ready" : mcp?.connection_state === "connecting" ? "Connecting local tools and loading the tool registry. Chat waits until tools are ready; no analysis is running." : "Stopped — start or reconnect the engine to use tools."}</p>
      <button type="button" class="action-button compact" data-web-action="refresh" ${busy}>Refresh status</button>
      <details class="web-notice"><summary>Privacy and external connections</summary><p>${escape(status?.engine_privacy_warning || "Provider keys stay in Python; OpenCode uses a temporary local relay token. Public release still requires the separate privacy review.")}</p><p>Files and databases stay local. Your activated model provider receives the prompts and tool results you send. No telemetry, public sharing or automatic model downloads are enabled.</p></details>
    </section>
    ` : ""}
    ${section.id === "providers" ? `
    ${panel.providers.length ? `<section class="web-settings-card"><div class="web-card-title"><h2>Saved connections</h2><button class="action-button compact" type="button" data-web-action="new-connection" ${busy}>Add connection</button></div>
      <div class="web-provider-list">${panel.providers.map((row) => `<div class="web-provider-row"><div><strong>${escape(row.label)}</strong><span>${escape(row.default_model)} · ${row.enabled ? "Active" : "Inactive"} · ${row.kind === "local" ? "Local endpoint" : row.connected ? row.credential_storage === "memory-only" ? "Key in memory" : "Key saved" : "Key required"}</span></div><div class="web-button-row"><button class="action-button compact" data-web-action="edit" data-provider="${escape(row.provider_id)}" ${busy}>Edit</button><button class="action-button compact" data-web-action="activate" data-provider="${escape(row.provider_id)}" ${busy || (row.enabled ? "disabled" : "")}>Activate</button><button class="action-button compact" data-web-action="remove" data-provider="${escape(row.provider_id)}" ${busy}>Remove</button></div></div>`).join("")}</div></section>` : ""}
    <section class="web-settings-card"><h2>${editing ? "Edit connection" : "Add connection"}</h2><p>Choose a provider, search its available models and enable the ones you want. API keys are never returned to the interface.</p>
      <form id="web-provider-form" data-credential-scope="${escape(JSON.stringify([draft.id, kind, draft.endpoint]))}" autocomplete="off"><div class="web-form-grid">
        <label>Provider<select name="kind">${catalog.map((row) => `<option value="${escape(row.kind)}" ${row.kind === kind ? "selected" : ""}>${escape(row.label)}</option>`).join("")}</select></label>
        <label>Connection name<input name="label" required maxlength="120" placeholder="My research provider" value="${escape(draft.label)}"></label>
        <label class="web-full">Endpoint <span class="reference-subtle">The selected provider receives model-list requests; prompts and tool results only when you use Chat</span><input name="endpoint" type="url" placeholder="https://provider.example/v1" value="${escape(draft.endpoint)}"></label>
        <label>API key<input name="key" type="password" autocomplete="new-password" placeholder="${editing?.connected ? "Key saved — leave blank to keep it" : "Enter a key to save for future launches"}"></label>
        ${preset?.key_sources?.length ? `<label>Or use an existing .env key<select name="keySource"><option value="">${editing?.connected ? "Keep saved key / enter a replacement" : "Select an existing key"}</option>${preset.key_sources.map((source) => `<option value="${escape(source.id)}" ${draft.keySource === source.id ? "selected" : ""}>${escape(source.name)} (${escape(source.source)} .env)</option>`).join("")}</select><span class="reference-subtle">Save & activate imports this key into the private profile. Key values are never shown.</span></label>` : ""}
      </div><section id="web-model-picker">${renderModelPicker(panel)}</section>
      <div class="web-button-row"><button class="action-button primary compact" type="submit" ${busy}>${editing ? "Update" : "Save"} & activate</button></div></form>
      <details class="web-notice"><summary>API key storage & privacy</summary><p class="web-storage-note">${status?.credential_storage === "os-keychain" ? "Keys are saved in your operating-system credential store." : status?.credential_storage === "memory-only" ? "This older runtime keeps keys in memory only. Restart with the updated app to enable persistent .env storage." : "Keys are saved as plaintext in your private profile's .env file (owner-only permissions, excluded from Git). Connections and enabled models are restored after restart."} OpenCode receives only a temporary local relay token, never your real provider key.</p><p>Unsaved key inputs are cleared when you switch categories or close Settings.</p></details>
    </section>
    ` : ""}
    ${section.id === "instructions" ? `
    <section class="web-settings-card"><h2>GLESKit instructions</h2><p>The skill entrypoint, core rules and matching references are supplied with every request. Missing required files stop submission.</p>
      <form id="web-instructions-form"><label>Additional instructions <span class="reference-subtle">Sent to the model; do not paste credentials here.</span><textarea name="instructions" id="web-extra-instructions" rows="5" maxlength="32000" placeholder="Optional preferences for your own work">${escape(panel.draft ?? panel.settings?.extra_instructions ?? "")}</textarea></label><button type="submit" class="action-button compact" ${busy}>Save instructions</button></form>
      <details><summary>View managed configuration</summary><p>Read-only safety policy. Only your web profile is used; global OpenCode configuration is not imported.</p><pre>${escape(JSON.stringify(panel.settings?.managed_config || {}, null, 2))}</pre></details>
    </section>
    ` : ""}
    ${section.id === "workspace" ? `
    <section class="web-settings-card"><h2>Workspace storage</h2>
      <p class="web-storage-note">${status?.workspace_source === "configured" ? "Connected to the configured local workspace, shared with the desktop. Existing sessions are opened in place; capture still requires explicit activation." : status?.browsing_read_only ? "Browsing the configured workspace read-only. Capture and edits are disabled." : "Using the separate web workspace. Sessions from your configured desktop workspace are not shown here."}</p>
      <details><summary>Data locations</summary><p>Private web runtime profile: <code>${escape(status?.profile || "")}</code></p><p>Workspace: <code>${escape(status?.workspace || "")}</code></p><p>Switching workspaces does not copy or rewrite saved results. Session changes happen only through explicit actions. Close the desktop before editing the same session from the web app.</p></details>
      <div class="web-button-row"><button class="action-button compact" data-web-action="configured-workspace" ${busy || (status?.workspace_source === "configured" ? "disabled" : "")}>Use configured workspace</button><button class="action-button compact" data-web-action="browse-existing" ${busy || (!status?.existing_workspace_available || status?.browsing_read_only ? "disabled" : "")}>Browse configured workspace (read-only)</button><button class="action-button compact" data-web-action="web-workspace" ${busy || (status?.workspace_source === "separate" ? "disabled" : "")}>Use separate web workspace</button></div>
    </section>
    <section class="web-settings-card"><h2>Runtime checks</h2><div class="web-environment"><div><span>Python</span><strong>${escape(status?.python || "Checking…")}</strong></div><div><span>R executable</span><strong>${status?.rscript_available ? "Found" : "Not found — configure R before R-backed analysis"}</strong></div><div><span>Project R library</span><strong>${status?.r_library_available ? "Found" : "Not found"}</strong></div><div><span>MCP package</span><strong>${status?.mcp_available ? "Available" : "Install the MCP extra"}</strong></div></div>
      <button type="button" class="action-button compact" data-web-action="refresh" ${busy}>Refresh environment status</button>
    </section>
    <section class="web-settings-card"><h2>Gene references</h2>
      <div class="web-button-row"><button class="action-button compact" data-web-action="references" ${busy}>Download/update gene references</button></div><p class="reference-copy">This explicit action downloads the public HGNC reference bundle. It does not run an analysis or update your R packages.</p>
    </section>
    ` : ""}
  </main></div></div>`;
}

export function bindControls(panel, invoke, refresh, update) {
  async function action(work, message) {
    panel.loading = true; panel.error = ""; panel.message = ""; update();
    try { await work(); panel.message = message; }
    catch (error) { panel.error = error.message; }
    finally {
      const failure = panel.error;
      panel.loading = false;
      await refresh();
      if (failure) { panel.error = failure; update(); }
    }
  }
  document.querySelectorAll("[data-web-section]").forEach((button) => button.addEventListener("click", () => {
    if (!SETTINGS_SECTIONS.some((row) => row.id === button.dataset.webSection) || panel.section === button.dataset.webSection) return;
    panel.section = button.dataset.webSection;
    update(); // Category navigation reads no backend state and never starts a service.
  }));
  document.querySelector("#web-extra-instructions")?.addEventListener("input", (event) => { panel.draft = event.target.value; });
  bindModelPicker(panel, invoke);
  for (const name of ["label", "endpoint"]) document.querySelector(`#web-provider-form [name="${name}"]`)?.addEventListener("input", (event) => {
    if (panel.providerDraft) panel.providerDraft[name] = event.target.value;
  });
  document.querySelector('#web-provider-form [name="keySource"]')?.addEventListener("change", (event) => {
    panel.providerDraft.keySource = event.target.value;
    panel.modelRequestSequence = (panel.modelRequestSequence || 0) + 1;
    panel.modelLoading = false; panel.modelScope = ""; panel.modelOptions = [];
    const key = document.querySelector('#web-provider-form [name="key"]');
    if (event.target.value && key) key.value = "";
    update();
  });
  document.querySelector('#web-provider-form [name="key"]')?.addEventListener("input", (event) => {
    if (!event.target.value || !panel.providerDraft) return;
    panel.providerDraft.keySource = "";
    const selector = document.querySelector('#web-provider-form [name="keySource"]');
    if (selector) selector.value = "";
  });
  document.querySelector('#web-provider-form [name="kind"]')?.addEventListener("change", (event) => {
    panel.providerKind = event.target.value;
    const saved = panel.editProvider === null ? null : panel.providers.find((row) => row.kind === panel.providerKind && row.enabled)
      || panel.providers.find((row) => row.kind === panel.providerKind);
    panel.editProvider = saved?.provider_id || null; panel.providerDraft = null; panel.modelOptions = []; update();
  });
  document.querySelector("#web-provider-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget, values = new FormData(form), key = String(values.get("key") || "");
    form.elements.namedItem("key").value = ""; values.delete("key");
    const kind = String(values.get("kind")), model = String(values.get("model") || "").trim();
    const models = [...new Set([model, ...(panel.providerDraft?.models || []), ...String(values.get("models") || "").split(/\r?\n/).map((value) => value.trim()).filter(Boolean)])].filter(Boolean);
    void action(async () => {
      const saved = await invoke("save_chat_provider", { providerId: panel.editProvider || null, kind, label: String(values.get("label") || ""),
        endpoint: String(values.get("endpoint") || "").trim() || null, models, defaultModel: model, apiKey: key || null,
        keySource: String(values.get("keySource") || "") || null, enabled: true });
      panel.editProvider = saved?.provider_id || null;
      panel.providerDraft = null;
    }, "Model connection saved. Open Chat to use it; no model request has been sent.");
  });
  document.querySelector("#web-instructions-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = String(new FormData(event.currentTarget).get("instructions") || "");
    void action(() => invoke("save_web_settings", { extraInstructions: text }), "Instructions saved in your web profile only.");
  });
  document.querySelectorAll("[data-web-action]").forEach((button) => button.addEventListener("click", () => {
    const kind = button.dataset.webAction;
    if (kind === "refresh") return void refresh();
    if (kind === "new-connection") { panel.editProvider = null; panel.providerDraft = null; panel.modelOptions = []; update(); return; }
    if (kind === "edit") { panel.editProvider = button.dataset.provider; panel.providerDraft = null; panel.modelOptions = []; update(); return; }
    if (kind === "take-control" && !window.confirm("Take control of this local profile? Other browser tabs will become read-only.")) return;
    if (kind === "remove" && !window.confirm("Remove this connection and its saved key? Conversations will be kept.")) return;
    if (kind === "references" && !window.confirm("Download/update the public HGNC reference bundle now?")) return;
    if (["browse-existing", "web-workspace", "configured-workspace"].includes(kind)) {
      if (!window.confirm("Switch workspace? Capture will be disabled and the app-owned engine will stop once so MCP cannot keep the old workspace path. The browser page will refresh. No saved results will be copied or rewritten by switching.")) return;
      return void action(async () => {
        await invoke(kind === "browse-existing" ? "browse_existing_workspace" : kind === "configured-workspace" ? "use_configured_workspace" : "use_web_workspace", {});
        window.location.reload();
      }, "Workspace view changed.");
    }
    const row = panel.providers.find((value) => value.provider_id === button.dataset.provider);
    const commands = { start: "start_web_engine", stop: "stop_web_engine", reconnect: "reconnect_chat_runtime", references: "update_references", "take-control": "take_control" };
    void action(() => kind === "remove" ? invoke("delete_chat_provider", { providerId: row.provider_id })
      : kind === "activate" ? invoke("save_chat_provider", { providerId: row.provider_id, kind: row.kind, label: row.label,
          endpoint: row.endpoint, models: row.models, defaultModel: row.default_model, apiKey: null, enabled: true })
      : invoke(commands[kind], {}), kind === "stop" ? "OpenCode stopped. Your local workspace remains available." : "Operation completed. Status has been refreshed.");
  }));
}
