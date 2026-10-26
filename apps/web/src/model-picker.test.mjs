import test from "node:test";
import assert from "node:assert/strict";
import { filterModels, providerDraft, renderModelPicker, bindModelPicker } from "./model-picker.js";

const models = [
  { id: "deepseek/fixture-flash", label: "DeepSeek Fixture Flash" },
  { id: "deepseek/fixture-pro", label: "DeepSeek Fixture Pro" },
  { id: "other/fixture", label: "Other Fixture" },
];

function fixture(panel) {
  const callbacks = {}, fields = {}, notices = [];
  for (const [name, value] of Object.entries({ key: "", label: "Fixture", endpoint: "https://openrouter.ai/api/v1" })) {
    fields[name] = { value, addEventListener(type, fn) { callbacks[name + ":" + type] = fn; } };
  }
  const results = { innerHTML: "" };
  const root = { innerHTML: "", addEventListener(type, fn) { callbacks[type] = fn; }, querySelector: () => results };
  const form = { elements: { namedItem: (name) => fields[name] } };
  const original = globalThis.document;
  globalThis.document = { dispatchEvent: (event) => notices.push(event), querySelector: (selector) => selector === "#web-model-picker" ? root : selector === "#web-provider-form" ? form : null };
  return { callbacks, fields, results, root, notices, restore() { globalThis.document = original; } };
}

test("catalog errors use the common modal channel instead of an inline error block", async () => {
  const panel = { providers: [], writable: true };
  providerDraft(panel, null, { endpoint: "https://openrouter.ai/api/v1" });
  const f = fixture(panel);
  try {
    bindModelPicker(panel, async () => { throw new Error("Synthetic catalog error"); });
    await new Promise(setImmediate);
    assert.equal(panel.modelError, "Synthetic catalog error");
    assert.equal(f.notices[0].type, "gleskit:notice");
    assert.equal(f.notices[0].detail.severity, "error");
    assert.doesNotMatch(f.root.innerHTML, /signal-banner error/);
  } finally { f.restore(); }
});

test("selected existing key is sent as a source name, not copied into renderer state", async () => {
  const panel = { providers: [], writable: true };
  const draft = providerDraft(panel, null, { endpoint: "https://openrouter.ai/api/v1" });
  draft.keySource = "project:OPENROUTER_API_KEY";
  const f = fixture(panel), calls = [];
  try {
    bindModelPicker(panel, async (_, args) => { calls.push(args); return models; });
    await new Promise(setImmediate);
    assert.equal(calls[0].keySource, draft.keySource);
    assert.equal(calls[0].apiKey, null);
    assert.equal(panel.providerDraft.keySource, "project:OPENROUTER_API_KEY");
  } finally { f.restore(); }
});

test("model search handles spaces, case, punctuation and all catalog rows", () => {
  assert.deepEqual(filterModels(models, "deep seek"), models.slice(0, 2));
  assert.deepEqual(filterModels(models, "DEEPSEEK fixture FLASH"), models.slice(0, 1));
  assert.deepEqual(filterModels(models, "deepseek flash"), models.slice(0, 1));
  assert.deepEqual(filterModels(models, "missing"), []);
  const many = [...Array.from({ length: 4000 }, (_, i) => ({ id: "other-" + i })), ...models];
  assert.equal(filterModels(many, "deep seek").length, 2);
});

test("unsaved provider loads a searchable catalog once without saving or inference", async () => {
  const panel = { providers: [], writable: true };
  providerDraft(panel, null, { endpoint: "https://openrouter.ai/api/v1", label: "OpenRouter" });
  const f = fixture(panel), calls = [];
  try {
    bindModelPicker(panel, async (command, args) => { calls.push({ command, args }); return models; });
    await new Promise(setImmediate);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].command, "fetch_provider_models");
    assert.equal(calls[0].args.providerId, null);
    assert.equal(calls[0].args.apiKey, null);
    assert.match(f.root.innerHTML, /DeepSeek Fixture Flash/);
    f.callbacks.input({ target: { id: "web-model-search", value: "deep seek" } });
    assert.match(f.results.innerHTML, /DeepSeek Fixture Flash/);
    assert.doesNotMatch(f.results.innerHTML, /Other Fixture/);
    bindModelPicker(panel, async () => { throw new Error("Cached navigation must not fetch again"); });
    await new Promise(setImmediate);
    assert.equal(calls.length, 1);
    let prevented = false;
    f.callbacks.keydown({ key: "Enter", target: { id: "web-model-search" }, preventDefault() { prevented = true; } });
    assert.equal(prevented, true, "Searching must not submit the provider form");
  } finally { f.restore(); }
});

test("multi-selection survives filtering and updates the default without touching a key", () => {
  const panel = { providers: [], writable: false };
  const draft = providerDraft(panel, null, { endpoint: "https://openrouter.ai/api/v1" });
  const f = fixture(panel);
  f.fields.key.value = "synthetic-private-ui-key";
  try {
    bindModelPicker(panel, () => assert.fail("Checkbox/search actions must not call the server"));
    f.callbacks.change({ target: { checked: true, value: models[0].id, hasAttribute: () => true } });
    f.callbacks.input({ target: { id: "web-model-search", value: "other" } });
    f.callbacks.change({ target: { checked: true, value: models[2].id, hasAttribute: () => true } });
    f.callbacks.change({ target: { name: "model", value: models[2].id } });
    assert.deepEqual(draft.models, [models[0].id, models[2].id]);
    assert.equal(draft.defaultModel, models[2].id);
    assert.equal(f.fields.key.value, "synthetic-private-ui-key");
    assert.doesNotMatch(JSON.stringify(panel), /synthetic-private-ui-key/);
  } finally { f.restore(); }
});

test("a late catalog from the previous provider cannot replace current results", async () => {
  const panel = { providers: [], writable: true };
  providerDraft(panel, null, { endpoint: "https://openrouter.ai/api/v1" });
  const f = fixture(panel);
  let finish;
  try {
    bindModelPicker(panel, () => new Promise((resolve) => { finish = resolve; }));
    panel.providerKind = "anthropic";
    providerDraft(panel, null, { endpoint: "https://api.anthropic.com/v1" });
    finish(models);
    await new Promise(setImmediate);
    assert.equal(panel.providerDraft.kind, "anthropic");
    assert.equal(panel.modelScope, "");
    assert.equal(panel.modelLoading, false);
  } finally { f.restore(); }
});

test("provider-supplied model names and IDs are escaped", () => {
  const panel = { providers: [] };
  const draft = providerDraft(panel, null, {});
  draft.models = ['<img src=x onerror="bad">'];
  draft.defaultModel = draft.models[0];
  assert.doesNotMatch(renderModelPicker(panel), /<img/);
});

for (const [kind, endpoint] of Object.entries({ openrouter: "https://openrouter.ai/api/v1", openai: "https://api.openai.com/v1",
  anthropic: "https://api.anthropic.com/v1", "openai-compatible": "https://custom.example/v1", local: "http://127.0.0.1:11434/v1" })) {
  test(`${kind} loads its catalog automatically and exposes source information`, async () => {
    const panel = { providers: [], writable: true, providerKind: kind };
    providerDraft(panel, null, { endpoint });
    const f = fixture(panel), calls = [];
    f.fields.endpoint.value = endpoint;
    try {
      bindModelPicker(panel, async (command, args) => {
        calls.push({ command, args });
        return { models, source: kind + " catalog", updated_at: "2026-10-21T00:00:00Z", requires_key_for_chat: kind !== "local" };
      });
      await new Promise(setImmediate);
      assert.equal(calls.length, 1);
      assert.equal(calls[0].command, "fetch_provider_models");
      assert.equal(calls[0].args.kind, kind);
      assert.equal(calls[0].args.endpoint, endpoint);
      assert.equal(calls[0].args.includeMetadata, true);
      assert.match(f.root.innerHTML, /Source:/);
      assert.match(f.root.innerHTML, /DeepSeek Fixture Flash/);
    } finally { f.restore(); }
  });
}

test("entering a key and clicking refresh does not launch two overlapping fetches", async () => {
  const panel = { providers: [], writable: true, providerKind: "openai" };
  providerDraft(panel, null, { endpoint: "https://api.openai.com/v1" });
  const f = fixture(panel), calls = [];
  f.fields.endpoint.value = "https://api.openai.com/v1";
  let finish;
  try {
    bindModelPicker(panel, async (command, args) => {
      calls.push(args);
      return calls.length === 1 ? models : await new Promise((resolve) => { finish = resolve; });
    });
    await new Promise(setImmediate);
    f.fields.key.value = "synthetic-private-updated-key";
    f.callbacks["key:change"]();
    f.callbacks.click({ target: { closest: () => ({ dataset: { modelAction: "load" } }) } });
    assert.equal(calls.length, 2, "Public fetch plus one authenticated fetch, not two authenticated fetches");
    assert.equal(calls[1].apiKey, "synthetic-private-updated-key");
    assert.doesNotMatch(JSON.stringify(panel), /synthetic-private-updated-key/);
    finish({ models, source: "Provider API", requires_key_for_chat: false });
    await new Promise(setImmediate);
    assert.equal(panel.modelLoading, false);
  } finally { f.restore(); }
});

test("an edited custom endpoint triggers discovery without saving first", async () => {
  const panel = { providers: [], writable: true, providerKind: "openai-compatible" };
  providerDraft(panel, null, { endpoint: "" });
  const f = fixture(panel), calls = [];
  f.fields.endpoint.value = "";
  try {
    bindModelPicker(panel, async (command, args) => { calls.push(args); return models; });
    assert.equal(calls.length, 0);
    f.fields.endpoint.value = "https://custom.example/v1";
    f.callbacks["endpoint:change"]();
    await new Promise(setImmediate);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].endpoint, "https://custom.example/v1");
    assert.equal(calls[0].providerId, null);
  } finally { f.restore(); }
});
