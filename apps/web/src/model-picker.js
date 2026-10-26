const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const normalize = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");

export function filterModels(models, query) {
  const needle = normalize(query);
  const tokens = String(query || "").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  return models.filter((row) => !needle || normalize(row.id).includes(needle) || normalize(row.label).includes(needle)
    || tokens.every((token) => `${row.id} ${row.label || ""}`.toLowerCase().includes(token)));
}

export function providerDraft(panel, editing, preset) {
  const kind = editing?.kind || panel.providerKind || "openrouter";
  const id = editing?.provider_id || "new:" + kind;
  if (panel.providerDraft?.id !== id) {
    panel.providerDraft = { id, kind, label: editing?.label || preset?.label || kind,
      endpoint: editing?.endpoint || preset?.endpoint || "", models: [...(editing?.models || [])],
      defaultModel: editing?.default_model || "", keySource: "" };
    panel.modelQuery = "";
    panel.modelScope = "";
    panel.modelInfo = null;
    panel.modelError = "";
    panel.modelLoading = false;
  }
  return panel.providerDraft;
}

function scopeFor(draft) { return JSON.stringify([draft.id, draft.kind, draft.endpoint, draft.keySource || ""]); }
function optionsFor(panel) { return panel.modelScope === scopeFor(panel.providerDraft) ? panel.modelOptions || [] : []; }

function applyCatalog(panel, value) {
  panel.modelOptions = Array.isArray(value) ? value : value.models;
  panel.modelInfo = Array.isArray(value) ? null : value;
}

function renderResults(panel) {
  const options = optionsFor(panel), matches = filterModels(options, panel.modelQuery);
  const selected = new Set(panel.providerDraft.models);
  const shown = matches.slice(0, panel.modelLimit || 60);
  return `<p class="web-model-count" role="status">${matches.length} matching / ${options.length} available · ${selected.size} enabled</p>
    <div class="web-model-list" role="group" aria-label="Available models">${shown.map((row) => `<label class="web-model-option"><input type="checkbox" data-web-model value="${escape(row.id)}" ${selected.has(row.id) ? "checked" : ""}><span><strong>${escape(row.label)}</strong><small>${escape(row.id)}</small></span></label>`).join("")
      || `<p class="reference-copy">${panel.modelLoading ? "Loading model metadata…" : panel.modelError ? "The catalog could not be loaded. Your enabled models are unchanged." : options.length ? "No matches. Try another model name." : panel.providerDraft.kind === "openai-compatible" && !panel.providerDraft.endpoint ? "Enter your server endpoint to discover its models." : "No models returned yet. Check the server endpoint or refresh the catalog."}</p>`}</div>
    ${matches.length > shown.length ? `<button type="button" class="action-button compact" data-model-action="more">Show more (${matches.length - shown.length} remaining)</button>` : ""}`;
}

export function renderModelPicker(panel) {
  const draft = panel.providerDraft, options = optionsFor(panel);
  const labelFor = (id) => options.find((row) => row.id === id)?.label || id;
  return `<div class="web-model-heading"><div><h3>Available models</h3><p>Search the provider's catalog and enable the models you want in Chat.</p></div><button type="button" class="action-button compact" data-model-action="load" ${panel.modelLoading ? "disabled" : ""}>${panel.modelLoading ? "Loading…" : options.length ? "Refresh models" : "Load models"}</button></div>
    ${panel.modelInfo && panel.modelScope === scopeFor(draft) ? `<p class="reference-copy" role="status">Source: ${escape(panel.modelInfo.source)}. ${panel.modelInfo.updated_at ? `Fetched ${escape(new Date(panel.modelInfo.updated_at).toLocaleString())}.` : ""} ${panel.modelInfo.requires_key_for_chat ? "An API key is still required for Chat. Enter a key and refresh to query account-specific availability." : "Model listing does not guarantee inference permissions."}</p>` : ""}
    ${panel.modelLoading ? `<p class="reference-copy" role="status">Fetching current model metadata. Without a key, built-in OpenAI and Anthropic connections use the public models.dev catalog. Custom/local connections query their server. No inference request is sent.</p>` : ""}
    <label>Search models<input type="search" id="web-model-search" placeholder="Search models, e.g. deep seek" value="${escape(panel.modelQuery || "")}" autocomplete="off"></label>
    <div id="web-model-results">${renderResults(panel)}</div>
    <div class="web-enabled-models" aria-label="Enabled models">${draft.models.map((id) => `<button type="button" class="action-button compact" data-model-action="remove" data-model-id="${escape(id)}" aria-label="Remove ${escape(id)}">${escape(labelFor(id))} ×</button>`).join("")}</div>
    <label>Default model<select name="model" required><option value="">Select a model above</option>${draft.models.map((id) => `<option value="${escape(id)}" ${draft.defaultModel === id ? "selected" : ""}>${escape(labelFor(id))}</option>`).join("")}</select></label>
    <details><summary>Model missing or endpoint has no catalog?</summary><label>Exact model ID<input id="web-manual-model" maxlength="200" placeholder="Exact provider model ID"></label><button type="button" class="action-button compact" data-model-action="manual">Add model ID</button></details>`;
}

export function bindModelPicker(panel, invoke) {
  const root = document.querySelector("#web-model-picker");
  if (!root) return;
  const form = document.querySelector("#web-provider-form");
  const draft = panel.providerDraft;
  const refresh = () => {
    const current = document.querySelector("#web-model-picker");
    if (panel.providerDraft === draft && current) current.innerHTML = renderModelPicker(panel);
  };
  const results = () => {
    const target = root.querySelector("#web-model-results");
    if (target) target.innerHTML = renderResults(panel);
  };
  const readMetadata = () => {
    for (const name of ["label", "endpoint"]) {
      const field = form?.elements.namedItem(name);
      if (field) draft[name] = field.value;
    }
  };
  async function load(force = false) {
    readMetadata();
    const scope = scopeFor(draft);
    panel.modelCatalogCache ||= new Map();
    const key = form?.elements.namedItem("key")?.value || "";
    const cached = panel.modelCatalogCache.get(scope);
    if (!force && !key && cached && Date.now() - cached.at < 300000) {
      panel.modelScope = scope; applyCatalog(panel, cached.value);
      panel.modelError = ""; panel.modelLoading = false; refresh(); return;
    }
    if (panel.modelLoading && panel.modelRequestScope === scope) return;
    const sequence = (panel.modelRequestSequence || 0) + 1;
    panel.modelRequestSequence = sequence;
    panel.modelRequestScope = scope;
    panel.modelScope = scope;
    panel.modelOptions = [];
    panel.modelInfo = null;
    panel.modelLoading = true; panel.modelError = ""; refresh();
    try {
      const models = await invoke("fetch_provider_models", { providerId: panel.editProvider || null,
        kind: draft.kind, endpoint: draft.endpoint || null, apiKey: key || null, keySource: draft.keySource || null, force, includeMetadata: true });
      if (panel.providerDraft !== draft || panel.modelRequestSequence !== sequence || scopeFor(draft) !== scope) return;
      applyCatalog(panel, models);
      if (panel.modelCatalogCache.size >= 32) panel.modelCatalogCache.delete(panel.modelCatalogCache.keys().next().value);
      panel.modelCatalogCache.set(scope, { at: Date.now(), value: models });
    } catch (error) {
      if (panel.providerDraft === draft && panel.modelRequestSequence === sequence && scopeFor(draft) === scope) {
        panel.modelError = error.message;
        document.dispatchEvent(new CustomEvent("gleskit:notice", { detail: { severity: "error", message: error.message } }));
      }
    } finally {
      if (panel.providerDraft === draft && panel.modelRequestSequence === sequence) { panel.modelLoading = false; refresh(); }
    }
  }
  root.addEventListener("input", (event) => {
    if (event.target.id !== "web-model-search") return;
    panel.modelQuery = event.target.value; panel.modelLimit = 60; results();
  });
  root.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.id === "web-model-search") event.preventDefault();
    if (event.key === "Enter" && event.target.id === "web-manual-model") {
      event.preventDefault(); root.querySelector('[data-model-action="manual"]').click();
    }
  });
  root.addEventListener("change", (event) => {
    const target = event.target;
    if (target.name === "model") { draft.defaultModel = target.value; return; }
    if (!target.hasAttribute("data-web-model")) return;
    draft.models = target.checked ? [...new Set([...draft.models, target.value])] : draft.models.filter((id) => id !== target.value);
    if (!draft.models.includes(draft.defaultModel)) draft.defaultModel = draft.models[0] || "";
    refresh();
  });
  root.addEventListener("click", (event) => {
    const button = event.target.closest("[data-model-action]");
    if (!button) return;
    const action = button.dataset.modelAction;
    if (action === "load") { void load(true); return; }
    if (action === "more") { panel.modelLimit = (panel.modelLimit || 60) + 100; results(); return; }
    if (action === "remove") draft.models = draft.models.filter((id) => id !== button.dataset.modelId);
    if (action === "manual") {
      const field = root.querySelector("#web-manual-model"), value = field.value.trim();
      if (!value) return;
      draft.models = [...new Set([...draft.models, value])];
    }
    if (!draft.models.includes(draft.defaultModel)) draft.defaultModel = draft.models[0] || "";
    refresh();
  });
  form?.elements.namedItem("key")?.addEventListener("change", () => {
    panel.modelRequestSequence = (panel.modelRequestSequence || 0) + 1;
    panel.modelLoading = false;
    panel.modelCatalogCache?.delete(scopeFor(draft));
    void load(true);
  });
  form?.elements.namedItem("endpoint")?.addEventListener("change", () => {
    readMetadata(); panel.modelScope = ""; panel.modelOptions = []; panel.modelError = "";
    panel.modelRequestSequence = (panel.modelRequestSequence || 0) + 1;
    panel.modelLoading = false; panel.modelInfo = null; refresh();
    if (panel.writable && draft.endpoint.trim()) void load();
  });
  if (panel.writable && !panel.loading && draft.endpoint.trim()) void load();
}
