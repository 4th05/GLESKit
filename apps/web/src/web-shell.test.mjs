import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { randomUUID } from "node:crypto";
import { renderControls, bindControls } from "./web-controls.js";

const source = readFileSync(new URL("./main.js", import.meta.url), "utf8");

test("Settings escapes provider, instruction and status content", () => {
  const unsafe = '<img src=x onerror="bad()">';
  const html = renderControls({ loading: false, writable: true, error: unsafe,
    status: { engine: { error: unsafe } }, settings: { extra_instructions: unsafe },
    providers: [{ provider_id: unsafe, label: unsafe, default_model: unsafe }] });
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
  assert.match(html, /private profile's .env file/);
});

test("Settings identifies the active workspace and exposes configured workspace connection", () => {
  const html = renderControls({ section: "workspace", providers: [], writable: true, status: { workspace_source: "configured" } });
  assert.match(html, /Connected to the configured local workspace/);
  assert.match(html, /data-web-action="configured-workspace" disabled/);
  const separate = renderControls({ section: "workspace", providers: [], writable: true, status: { workspace_source: "separate" } });
  assert.match(separate, /Sessions from your configured desktop workspace are not shown here/);
  assert.match(separate, /data-web-action="configured-workspace" >/);
});

test("a Settings operation failure stays visible after the status refresh", async () => {
  let click, refreshed;
  const done = new Promise((resolve) => { refreshed = resolve; });
  const button = { dataset: { webAction: "start" }, addEventListener: (_, callback) => { click = callback; } };
  const original = globalThis.document;
  globalThis.document = { querySelector: () => null, querySelectorAll: () => [button] };
  const panel = { providers: [] };
  try {
    bindControls(panel, async () => { throw new Error("Configure a provider first"); },
      async () => { panel.error = ""; }, () => { if (panel.error) refreshed(); });
    click();
    await done;
    // Let action's finally complete, including the refresh that clears errors.
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(panel.error, "Configure a provider first");
    assert.equal(panel.loading, false);
  } finally { globalThis.document = original; }
});

test("web credential input is cleared before submission and not put in panel state", async () => {
  let submit, complete;
  const done = new Promise((resolve) => { complete = resolve; });
  const field = { value: "synthetic-fixture-token" };
  const values = new Map([["key", field.value], ["kind", "openai"], ["model", "fixture"], ["label", "Fixture"]]);
  const form = { elements: { namedItem: () => field }, addEventListener: (_, callback) => { submit = callback; } };
  const originalDocument = globalThis.document, originalFormData = globalThis.FormData;
  globalThis.document = { querySelector: (selector) => selector === "#web-provider-form" ? form : null, querySelectorAll: () => [] };
  globalThis.FormData = class { get(key) { return values.get(key); } delete(key) { values.delete(key); } };
  const panel = { providers: [] };
  try {
    bindControls(panel, async (command, args) => {
      assert.equal(command, "save_chat_provider");
      assert.equal(field.value, "");
      assert.equal(values.has("key"), false);
      assert.equal(args.apiKey, "synthetic-fixture-token");
      assert.equal(args.providerId, null, "A new connection must not overwrite the provider protocol's ID");
      assert.doesNotMatch(JSON.stringify(panel), /synthetic-fixture-token/);
    }, async () => { complete(); }, () => {});
    submit({ preventDefault() {}, currentTarget: form });
    await done;
    assert.equal(panel.error, "");
  } finally { globalThis.document = originalDocument; globalThis.FormData = originalFormData; }
});

test("Settings exposes named providers, distinct connections and actual MCP readiness", () => {
  const html = renderControls({ providers: [
    { provider_id: "first", kind: "openrouter", label: "First", models: ["vendor/a"], default_model: "vendor/a", connected: true },
    { provider_id: "second", kind: "openrouter", label: "Second", models: ["vendor/b"], default_model: "vendor/b", connected: true },
  ], writable: true, editProvider: "second", status: { engine: { running: true }, mcp: { connection_state: "connecting" } } });
  for (const provider of ["OpenRouter", "OpenAI", "Anthropic", "Local OpenAI-compatible"]) assert.match(html, new RegExp(provider));
  assert.match(html, /data-provider="first"/);
  assert.match(html, /data-provider="second"/);
  assert.match(html, /value="vendor\/b"/);
  const engine = renderControls({ section: "engine", providers: [], writable: true, status: { engine: { running: true }, mcp: { connection_state: "connecting" } } });
  assert.match(engine, /Connecting local tools/);
  assert.match(html, /temporary local relay token/);
});

test("Settings reopens the saved active connection and enabled models without exposing a key", () => {
  const panel = { providers: [{ provider_id: "saved", kind: "openrouter", label: "My saved connection", enabled: true,
    connected: true, credential_storage: "private-dotenv", models: ["vendor/first", "vendor/second"], default_model: "vendor/second" }],
    writable: true, status: { credential_storage: "private-dotenv" } };
  const html = renderControls(panel);
  assert.equal(panel.editProvider, "saved");
  assert.deepEqual(panel.providerDraft.models, ["vendor/first", "vendor/second"]);
  assert.equal(panel.providerDraft.defaultModel, "vendor/second");
  assert.match(html, /value="My saved connection"/);
  assert.match(html, /Key saved — leave blank to keep it/);
  assert.match(html, /Connections and enabled models are restored after restart/);
  assert.doesNotMatch(html, /name="key"[^>]*value=/);
  panel.editProvider = null;
  panel.providerDraft = null;
  renderControls(panel);
  assert.equal(panel.editProvider, null, "Explicit Add another connection must not reopen an existing one");
  assert.deepEqual(panel.providerDraft.models, []);
});

test("Settings offers existing .env variable names without exposing values", () => {
  const html = renderControls({ providers: [], writable: true, catalog: [{ kind: "openrouter", label: "OpenRouter", endpoint: "https://openrouter.ai/api/v1",
    key_sources: [{ id: "project:OPENROUTER_API_KEY", name: "OPENROUTER_API_KEY", source: "project" }] }] });
  assert.match(html, /name="keySource"/);
  assert.match(html, /value="project:OPENROUTER_API_KEY"/);
  assert.match(html, /Save & activate imports this key/);
  assert.doesNotMatch(html, /name="key"[^>]*value=/);
});

test("Settings opens over the current page and uses only read commands until an explicit action", async () => {
  const transport = readFileSync(new URL("./transport.web.js", import.meta.url), "utf8")
    .replace(/^import .*;$/gm, "").replace(/^export /gm, "");
  const requests = [], events = [];
  let binding, controller, running = false;
  const context = vm.createContext({ URL, URLSearchParams, crypto: { randomUUID },
    window: { location: { hash: "", pathname: "/" }, history: { replaceState() {} }, sessionStorage: { getItem: () => "fixture_tab" } },
    document: { body: { classList: { add() {} } }, dispatchEvent: (event) => events.push(event) },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    EventSource: class { addEventListener() {} }, renderControls: () => "settings",
    bindControls(panel, invoke, refresh) { binding = { panel, invoke, refresh }; },
    createSettingsDialog(options) {
      controller = { isOpen: false, refresh() {}, open() { this.isOpen = true; options.bind(); }, close() { this.isOpen = false; options.onClose(); } };
      return controller;
    },
    fetch: async (url) => {
      requests.push(url);
      const result = url.endsWith("get_web_status") ? { engine: { running }, mcp: { running } }
        : url.endsWith("list_chat_providers") ? [{ provider_id: "fixture", enabled: true }] : {};
      const value = url === "/api/session" ? { csrf: "fixture", runtime: { revision: 0 } }
        : url === "/api/control" ? { writable: true, revision: 0 } : { ok: true, result };
      return { ok: true, headers: { get: () => "0" }, json: async () => value };
    } });
  vm.runInContext(transport, context);
  context.openSystemPanel(); context.openSystemPanel();
  await new Promise(setImmediate);
  const reads = requests.filter((url) => url.startsWith("/api/commands/"));
  assert.equal(reads.length, 4, "A repeated Open must not reload Settings twice");
  assert.ok(reads.every((url) => /get_web_status|get_web_settings|list_chat_providers|get_provider_catalog/.test(url)));
  await binding.invoke("stop_web_engine"); await binding.refresh(); controller.close();
  assert.equal(events.filter((event) => event.type === "gleskit:settings-closed").at(-1).detail.chatChanged, false, "An explicit Stop must not request automatic restart");
  context.openSystemPanel(); await new Promise(setImmediate);
  await binding.invoke("save_chat_provider", { providerId: "fixture" });
  binding.panel.loading = true;
  controller.close(); // The save's final status refresh can finish after Close.
  const beforeRefresh = events.length;
  binding.panel.loading = false;
  await binding.refresh();
  assert.ok(events.slice(beforeRefresh).some((event) => event.type === "gleskit:settings-closed"), "Closed Settings must still publish completed changes");
  assert.ok(!requests.some((url) => /create_chat|send_chat|start_web_engine/.test(url)));
});

test("returning from Settings preserves the page and respects an intentional engine Stop", () => {
  const handlers = {}, state = { appView: "chat", chatProviders: [], chatBusy: false, mcpStatus: {} };
  let starts = 0;
  const context = vm.createContext({ state, isWebShell: true, render() {}, syncSelectedProviderAndModel() {},
    ensureChatReady() { starts++; return Promise.resolve(); }, formatError: String,
    document: { addEventListener: (name, callback) => { handlers[name] = callback; } } });
  vm.runInContext(source.slice(source.indexOf('document.addEventListener("gleskit:settings-closed"'), source.indexOf('document.addEventListener("gleskit:runtime-updated"')), context);
  const providers = [{ provider_id: "fixture", enabled: true }];
  handlers["gleskit:settings-closed"]({ detail: { providers, mcp: { running: false }, chatChanged: false } });
  assert.equal(starts, 0);
  assert.equal(state.appView, "chat");
  assert.equal(state.mcpStatus.running, false);
  handlers["gleskit:settings-closed"]({ detail: { providers, chatChanged: true } });
  assert.equal(starts, 1, "A provider change reconnects Chat only on return to Chat");
  state.appView = "workspace";
  handlers["gleskit:settings-closed"]({ detail: { providers, chatChanged: true } });
  assert.equal(starts, 1);
  assert.equal(state.appView, "workspace");
  handlers["gleskit:settings-closed"]({ detail: { providers: [], chatChanged: true } });
  assert.equal(starts, 1, "Removing the final provider must not start an engine");
});

test("chat toolbar hides fixed agent/skill badges and keeps accessible provider and model controls", () => {
  const main = source.slice(source.indexOf("function renderChatMain("), source.indexOf("function renderChatBootstrapBanner("));
  assert.doesNotMatch(main, /GLESKit Mode|renderChatSkillStrip/);
  assert.match(main, /aria-label="Select provider"/);
  const picker = source.slice(source.indexOf("function renderChatModelPicker("), source.indexOf("function commitChatModelQuery("));
  assert.doesNotMatch(picker, /chat-model-help|Model picker help/);
  assert.match(picker, /aria-label="Select model"/);
  assert.doesNotMatch(main, /renderChatAgentPicker|chat-agent-select/);
  const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  assert.match(css, /--chat-control-height: 36px/);
  assert.match(css, /\.chat-model-picker-row\s*\{[^}]*height: var\(--chat-control-height, 36px\)/);
});

test("browser reconnect refreshes its lease without takeover or replaying a chat write", async () => {
  const transport = readFileSync(new URL("./transport.web.js", import.meta.url), "utf8")
    .replace(/^import .*;$/gm, "").replace(/^export /gm, "");
  const callbacks = {}, requests = [], events = [];
  let sessions = 0;
  const context = vm.createContext({ URL, URLSearchParams, crypto: { randomUUID },
    window: { location: { hash: "", pathname: "/" }, history: { replaceState() {} }, sessionStorage: { getItem: () => "fixture_tab" } },
    document: { body: { classList: { add() {} } }, dispatchEvent: (event) => events.push(event) },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    EventSource: class { addEventListener(name, callback) { callbacks[name] = callback; } },
    fetch: async (url, init) => {
      requests.push({ url, body: init.body ? JSON.parse(init.body) : null });
      const runtime = { revision: sessions, capture_enabled: false, active_session_id: null };
      const value = url === "/api/session" ? { csrf: "csrf-" + ++sessions, runtime }
        : url === "/api/control" ? { writable: true, revision: sessions, runtime } : { ok: true, result: {} };
      return { ok: true, headers: { get: () => String(sessions) }, json: async () => value };
    } });
  vm.runInContext(transport, context);
  await context.invoke("get_web_status");
  callbacks.open(); callbacks.error(); callbacks.open();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(sessions, 2);
  assert.equal(requests.filter((row) => row.url === "/api/control").length, 2);
  assert.equal(requests.filter((row) => row.url.startsWith("/api/commands/")).length, 1);
  assert.ok(requests.filter((row) => row.url === "/api/control").every((row) => row.body.takeover !== true));
  assert.ok(events.some((event) => event.type === "gleskit:chat-reconcile"));
  assert.ok(events.some((event) => event.type === "gleskit:runtime-updated" && event.detail.runtime.capture_enabled === false));
});

test("browser Markdown rejects remote images, unregistered files and executable links", () => {
  const context = vm.createContext({ isWebShell: true, URL, window: { location: { origin: "http://127.0.0.1:8123" } },
    convertFileSrc: (path) => path === "/registered.png" ? "/api/files/opaque?inline=1" : "",
    escapeHtml: (value) => String(value).replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
    renderDirectionalGeneMarkdown: (value) => value });
  vm.runInContext(source.slice(source.indexOf("function renderInlineMarkdown("), source.indexOf("function bindMarkdownImageFallbacks(")), context);
  vm.runInContext(source.slice(source.indexOf("function normalizeExternalUrl("), source.indexOf("async function openExternalUrl(")), context);
  for (const value of ["https://external.example/tracker.png", "http://%", "/private-fixture.png", "data:image/svg+xml;base64,PHN2Zz4="])
    assert.equal(context.normalizeImageSrc(value).src, "");
  assert.equal(context.normalizeImageSrc("/registered.png").src, "/api/files/opaque?inline=1");
  for (const value of ["javascript:bad", "file:///private-fixture", "data:text/html,bad"])
    assert.doesNotMatch(context.renderInlineMarkdown(`[click](${value})`), /href=/);
  assert.match(context.renderInlineMarkdown("[safe](https://example.test/)"), /noopener noreferrer/);
});

test("browser transport serializes writes and never retries a failed mutation", async () => {
  const transport = readFileSync(new URL("./transport.web.js", import.meta.url), "utf8")
    .replace(/^import .*;$/gm, "").replace(/^export /gm, "");
  const calls = [];
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const context = vm.createContext({ URL, URLSearchParams, crypto: { randomUUID },
    window: { location: { hash: "", pathname: "/" }, history: { replaceState() {} },
      sessionStorage: { getItem: () => "fixture_tab", setItem() {} } },
    document: { body: { classList: { add() {} } }, dispatchEvent() {} },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    EventSource: class { addEventListener() {} },
    fetch: async (url) => {
      if (url === "/api/session") return response({ csrf: "fixture", runtime: { revision: 0 } });
      if (url === "/api/control") return response({ writable: true, revision: 0 });
      calls.push(url);
      if (calls.length === 1) { await gate; return response({ ok: false, error: { message: "failed once" } }); }
      return response({ ok: true, result: "second" });
    } });
  function response(value) { return { ok: true, headers: { get: () => "0" }, json: async () => value }; }
  vm.runInContext(transport, context);
  const first = context.invoke("create_session", { title: "first" });
  const second = context.invoke("create_session", { title: "second" });
  const failed = assert.rejects(first, /failed once/);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls.length, 1);
  release();
  await failed;
  assert.equal(await second, "second");
  assert.equal(calls.length, 2);
});

test("browser mode does not install the desktop selection heartbeat interval", () => {
  let intervals = 0;
  const context = vm.createContext({ state: { heartbeatTimer: null }, isWebShell: true,
    window: { setInterval: () => { intervals++; } } });
  vm.runInContext(source.slice(source.indexOf("function startHeartbeat("), source.indexOf("async function writeHeartbeat(")), context);
  context.startHeartbeat();
  assert.equal(intervals, 0);
});
