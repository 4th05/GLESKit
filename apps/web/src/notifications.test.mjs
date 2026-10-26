import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { NoticeQueue, initializeNotifications } from "./notifications.js";

test("notifications queue until explicitly acknowledged and do not reappear on render", () => {
  const shown = [], queue = new NoticeQueue((value) => shown.push(value));
  const entries = [{ source: "error", severity: "error", message: "Failed" }, { source: "success", message: "Saved" }];
  queue.update("main", entries);
  assert.equal(shown.length, 1);
  queue.update("main", entries);
  queue.close();
  assert.equal(shown.length, 2);
  assert.equal(queue.active.message, "Saved");
  queue.close(); queue.update("main", entries);
  assert.equal(queue.active, null);
  queue.update("main", []); queue.update("main", entries.slice(0, 1));
  assert.equal(shown.length, 3, "A genuinely new failure can notify again after the source cleared");
  queue.update("settings", entries.slice(0, 1));
  assert.equal(queue.pending.length, 0, "Identical simultaneous errors should not duplicate across sources");
});

test("acknowledgment can clear its source so a later identical action notifies again", () => {
  let message = "Saved";
  const shown = [], queue = new NoticeQueue((value) => shown.push(value));
  const sync = () => queue.update("main", [{ source: "success", severity: "success", message,
    acknowledge() { message = ""; sync(); } }]);
  sync(); queue.close();
  assert.equal(message, "");
  message = "Saved"; sync();
  assert.equal(shown.length, 2);
});

test("message categories map to the error, warning, positive and general palettes", () => {
  for (const [severity, expected] of [["error", "error"], ["warning", "warning"], ["success", "success"],
    ["positive", "success"], ["follow-up", "success"], ["info", "info"], [undefined, "info"], ["other", "info"]]) {
    const queue = new NoticeQueue();
    queue.push({ message: "Fixture", severity });
    assert.equal(queue.active.severity, expected);
  }
  const css = readFileSync(new URL("./web.css", import.meta.url), "utf8");
  for (const [severity, accent] of [["error", "#b42318"], ["warning", "#eab308"], ["success", "#067647"]])
    assert.ok(css.includes(`.gleskit-notification[data-severity="${severity}"] { --notice-accent: ${accent}`));
  assert.match(css, /\.gleskit-notification\s*\{\s*--notice-accent: #175cd3/);
  assert.match(css, /background: var\(--notice-tint\)/);
  assert.match(css, /background: var\(--notice-accent\); border-color: var\(--notice-accent\)/);
});

test("all severities use one native blocking dialog with text-only content and a Close button", () => {
  const handlers = {}, events = {}, title = {}, body = {}, button = { addEventListener(name, fn) { handlers[name] = fn; }, focus() {} };
  let shown = 0, appended = 0;
  const dialog = { dataset: {}, setAttribute(name, value) { this[name] = value; },
    querySelector(selector) { return selector === "button" ? button : selector === "h2" ? title : body; },
    addEventListener(name, fn) { events[name] = fn; }, showModal() { shown++; }, close() { events.close(); } };
  const doc = { createElement: () => dialog, body: { append() { appended++; } }, addEventListener(name, fn) { handlers[name] = fn; }, activeElement: { isConnected: true } };
  const queue = initializeNotifications(doc);
  queue.push({ severity: "error", message: "<script>not HTML</script>\n" + "long ".repeat(1000) });
  queue.push({ severity: "warning", message: "Warning" });
  assert.equal(shown, 1);
  assert.equal(dialog.role, "alertdialog");
  assert.equal(dialog["aria-modal"], "true");
  assert.match(body.textContent, /^<script>/);
  assert.equal(title.textContent, "Error");
  handlers.click();
  assert.equal(shown, 2);
  assert.equal(title.textContent, "Warning");
  handlers.click();
  assert.equal(queue.active, null);
  queue.push({ severity: "follow-up", message: "Next step ready" });
  assert.equal(dialog.dataset.severity, "success");
  assert.equal(title.textContent, "Done");
  handlers.click();
  queue.push({ message: "General information" });
  assert.equal(dialog.dataset.severity, "info");
  assert.equal(title.textContent, "Information");
  handlers.click();
  assert.equal(appended, 1);
  const css = readFileSync(new URL("./web.css", import.meta.url), "utf8");
  assert.match(css, /width: min\(560px/);
  assert.match(css, /height: min\(280px/);
  assert.ok(css.includes('.gleskit-notification[open] .action-button.primary {'), "Severity colors must override generic light/dark primary-button styles");
  assert.match(css, /#gleskit-notification-body[^}]*overflow-y: auto/s);
});
