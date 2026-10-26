import test from "node:test";
import assert from "node:assert/strict";
import { createSettingsDialog } from "./settings-dialog.js";
import { renderControls, bindControls, SETTINGS_SECTIONS } from "./web-controls.js";

function fixture() {
  const state = { section: "providers", scope: "fixture:official-endpoint" }, nodes = new Map(), listeners = {};
  let created = 0, closed = 0, markup = "", notice = false;
  const doc = { activeElement: null, body: { append() {} },
    createElement() { created++; return dialog; },
    getElementById: (id) => nodes.get("#" + id),
    querySelector: () => notice ? {} : null };
  const node = (values = {}) => ({ scrollTop: 0, ...values, focus() { doc.activeElement = this; },
    addEventListener(name, callback) { this[name] = callback; },
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; } });
  const dialog = { open: false, setAttribute() {}, dataset: {},
    contains: (item) => [...nodes.values()].includes(item), querySelector: (selector) => nodes.get(selector),
    addEventListener(name, callback) { listeners[name] = callback; },
    showModal() { this.open = true; }, close() { this.open = false; listeners.close(); },
    replaceChildren() { nodes.clear(); },
    set innerHTML(value) {
      markup = value; nodes.clear();
      nodes.set("[data-settings-section]", node({ dataset: { settingsSection: state.section } }));
      nodes.set("#web-settings-content", node());
      nodes.set("#web-settings-close", node({ id: "web-settings-close" }));
      nodes.set("#web-settings-section-title", node({ id: "web-settings-section-title" }));
      if (state.section === "providers") {
        const key = node({ name: "key", type: "password", value: "", selectionStart: 2, selectionEnd: 2 });
        nodes.set('[name="key"]', key);
        nodes.set("#web-provider-form", node({ dataset: { credentialScope: state.scope }, elements: { namedItem: () => key } }));
      }
    } };
  const controller = createSettingsDialog({ doc, render: () => JSON.stringify(state), bind() {}, onClose() { closed++; } });
  return { state, nodes, doc, dialog, controller, get created() { return created; }, get closed() { return closed; },
    get markup() { return markup; }, showNotice() { notice = true; } };
}

test("Settings is one blocking modal outside the page, with Close/Escape cleanup", () => {
  const f = fixture();
  f.controller.open(); f.controller.open();
  assert.equal(f.created, 1);
  assert.equal(f.controller.isOpen, true);
  assert.equal(f.doc.activeElement.id, "web-settings-close");
  f.nodes.get('[name="key"]').value = "synthetic-unsaved-token";
  f.nodes.get("#web-settings-close").click();
  assert.equal(f.controller.isOpen, false);
  assert.equal(f.nodes.size, 0);
  assert.equal(f.closed, 1);
  f.controller.open();
  assert.equal(f.created, 1);
  assert.equal(f.nodes.get('[name="key"]').value, "");
  f.dialog.close(); // Native Escape follows the same close event.
  assert.equal(f.closed, 2);
});

test("same-category status updates preserve focus, scroll and a DOM-only key", () => {
  const f = fixture(); f.controller.open();
  f.nodes.get("#web-settings-content").scrollTop = 130;
  const key = f.nodes.get('[name="key"]'); key.value = "synthetic-unsaved-token"; key.focus();
  f.controller.refresh();
  assert.equal(f.nodes.get("#web-settings-content").scrollTop, 130);
  assert.equal(f.doc.activeElement, f.nodes.get('[name="key"]'));
  assert.equal(f.doc.activeElement.value, "synthetic-unsaved-token");
  assert.doesNotMatch(f.markup, /synthetic-unsaved-token/);
  assert.doesNotMatch(JSON.stringify(f.state), /synthetic-unsaved-token/);
});

test("closing Settings returns keyboard focus to the global footer button", () => {
  const f = fixture();
  const opener = { focus() { f.doc.activeElement = this; } };
  const get = f.doc.getElementById;
  f.doc.getElementById = (id) => id === "open-settings-button" ? opener : get(id);
  f.controller.open(); f.controller.close();
  assert.equal(f.doc.activeElement, opener);
});

test("category/destination changes never carry an unsaved key into another form", () => {
  const f = fixture(); f.controller.open();
  f.nodes.get('[name="key"]').value = "synthetic-unsaved-token";
  f.state.scope = "fixture:other-endpoint"; f.controller.refresh();
  assert.equal(f.nodes.get('[name="key"]').value, "");
  f.nodes.get('[name="key"]').value = "synthetic-unsaved-token";
  f.nodes.get("#web-settings-content").scrollTop = 130;
  f.state.section = "instructions"; f.controller.refresh();
  assert.equal(f.nodes.get("#web-settings-content").scrollTop, 0);
  assert.equal(f.doc.activeElement.id, "web-settings-section-title");
  f.state.section = "providers"; f.controller.refresh();
  assert.equal(f.nodes.get('[name="key"]').value, "");
});

test("Settings refresh cannot steal focus from a notification on top", () => {
  const f = fixture(); f.controller.open();
  f.showNotice(); const close = {}; f.doc.activeElement = close;
  f.state.section = "engine"; f.controller.refresh();
  assert.equal(f.doc.activeElement, close);
});

test("categories are ordered by common tasks and mount only the selected controls", () => {
  assert.deepEqual(SETTINGS_SECTIONS.map((row) => row.id), ["providers", "instructions", "engine", "workspace"]);
  const markers = { providers: 'id="web-provider-form"', instructions: 'id="web-instructions-form"', engine: 'data-web-action="start"', workspace: 'data-web-action="configured-workspace"' };
  const panel = { providers: [], writable: true };
  for (const section of SETTINGS_SECTIONS) {
    panel.section = section.id;
    const html = renderControls(panel);
    assert.ok(html.includes(markers[section.id]));
    for (const [id, marker] of Object.entries(markers)) if (id !== section.id) assert.ok(!html.includes(marker));
    assert.ok(html.includes(`data-web-section="${section.id}" aria-current="page"`));
  }
});

test("sidebar clicks only change category and preserve model/instruction drafts", () => {
  const callbacks = {}, panel = { section: "providers", providers: [], providerDraft: { models: ["fixture"] }, draft: "Unsaved preference" };
  const original = globalThis.document;
  globalThis.document = { querySelector: () => null, querySelectorAll: (selector) => selector === "[data-web-section]" ? SETTINGS_SECTIONS.map((row) => ({ dataset: { webSection: row.id }, addEventListener: (_, callback) => { callbacks[row.id] = callback; } })) : [] };
  let updates = 0;
  try {
    bindControls(panel, () => assert.fail("Navigation must not invoke a backend command"), () => assert.fail("Navigation must not refresh services"), () => { updates++; });
    callbacks.instructions(); callbacks.engine(); callbacks.workspace();
    assert.equal(updates, 3);
    assert.equal(panel.section, "workspace");
    assert.deepEqual(panel.providerDraft.models, ["fixture"]);
    assert.equal(panel.draft, "Unsaved preference");
    callbacks.workspace(); assert.equal(updates, 3);
  } finally { globalThis.document = original; }
});
