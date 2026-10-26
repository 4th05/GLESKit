import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { renderControls } from "./web-controls.js";

const source = readFileSync(new URL("./main.js", import.meta.url), "utf8");
function fixture() {
  const state = { appView: "workspace", chatConversations: [], chatProviders: [], chatDraft: "", mcpStatus: { running: true } };
  const context = vm.createContext({ state, isWebShell: true, sidebarBrandIconUrl: "fixture.svg", escapeHtml: String,
    gearIcon: () => "<svg></svg>", refreshIcon: () => "<svg></svg>", renderChatConversationRow: () => "",
    renderThemeToggle: () => '<button data-action="toggle-theme">Theme</button>',
    getSelectedChatProvider: () => null, parseChatTurnDirectives: () => ({ inputDataRefs: [] }), isChatReady: () => true,
    renderChatAgentPicker: () => "", renderChatModelPicker: () => "", renderChatMessages: () => "", renderChatActivitySidebar: () => "",
    renderChatBootstrapBanner: () => "", upIcon: () => "", downIcon: () => "" });
  vm.runInContext(source.slice(source.indexOf("function renderSidebar("), source.indexOf("function renderSessionSnapshotBanner(")), context);
  vm.runInContext(source.slice(source.indexOf("function renderChatMain("), source.indexOf("function renderChatBootstrapBanner(")), context);
  return { state, context };
}

test("each major page has exactly one Settings entry in the footer above local-storage information", () => {
  const { state, context } = fixture();
  for (const page of ["workspace", "chat", "cohorts"]) {
    state.appView = page;
    const html = context.renderSidebar(null, null);
    assert.equal((html.match(/data-action="show-app-system"/g) || []).length, 1);
    assert.ok(html.indexOf('class="web-sidebar-footer"') < html.indexOf('id="open-settings-button"'));
    assert.ok(html.indexOf('id="open-settings-button"') < html.indexOf("On your computer"));
    assert.match(html, /aria-haspopup="dialog" aria-controls="web-settings-dialog"/);
    assert.doesNotMatch(html, /id="open-chat-settings"/);
  }
  assert.doesNotMatch(context.renderAppNavigation(), /Settings|show-app-system/);
});

test("web page toolbars have no duplicate Settings or refresh/update actions", () => {
  const { state, context } = fixture();
  for (const page of ["workspace", "cohorts"]) {
    state.appView = page;
    const html = context.renderTopbarActions();
    assert.match(html, /toggle-theme/);
    assert.doesNotMatch(html, /refresh-page|show-app-system|update-references/);
  }
  state.appView = "chat";
  const chat = context.renderChatMain(null);
  assert.doesNotMatch(chat, /open-chat-settings|refresh-page|Refresh chat/);
  assert.match(chat, /toggle-chat-activity-sidebar/);
  assert.match(chat, /aria-label="Send message"/);
  assert.match(context.renderChatSidebarSummary(), /New Chat/);
});

test("engine reconnection and data maintenance are available in Settings, not Chat banners", () => {
  const { state, context } = fixture();
  vm.runInContext(source.slice(source.indexOf("function renderChatBootstrapBanner("), source.indexOf("function renderChatConversationRow(")), context);
  state.chatBootstrapState = "failed";
  assert.equal(context.renderChatBootstrapBanner(), "");
  state.chatBootstrapState = "ready"; state.mcpStatus = { running: false, connection_state: "stopped" };
  assert.equal(context.renderChatBootstrapBanner(), "");
  state.mcpStatus.connection_state = "connecting";
  assert.match(context.renderChatBootstrapBanner(), /Connecting local GLESKit tools/);
  for (const [section, expected] of [["engine", /data-web-action="reconnect"/], ["workspace", /data-web-action="references"/], ["providers", /data-model-action="load"/]])
    assert.match(renderControls({ section, providers: [], writable: true }), expected);
});

test("the footer Settings control stays visible on narrow screens", () => {
  const css = readFileSync(new URL("./web.css", import.meta.url), "utf8");
  const narrow = css.slice(css.indexOf("@media (max-width: 600px)"));
  assert.match(narrow, /\.web-sidebar-footer\s*\{[^}]*display: flex/);
  assert.doesNotMatch(narrow, /\.web-sidebar-footer[^}]*display: none/);
  assert.match(css, /\.web-sidebar-content\s*\{[^}]*overflow-y: auto/);
});
