import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("./main.js", import.meta.url), "utf8");
const refreshSource = source.slice(source.indexOf("async function refreshSelectedChatConversation("), source.indexOf("function handleChatTurnEvent("));

function historyFixture(invoke) {
  const state = { selectedChatId: "old", activeChatTurn: { turn_id: "old-turn" }, chatBusy: true,
    chatMessages: [{ content_markdown: "Old conversation" }], chatTurnEvents: [], chatConversations: [],
    chatConversationLoadGeneration: 0, chatDraft: "Keep draft" };
  const scheduled = [];
  const context = vm.createContext({ state, invoke, isWebShell: true, render() {}, clearChatConversationRefreshTimer() {},
    normalizeChatTurnEvent: (event) => event, syncSelectedProviderAndModel() {}, queueChatScrollToLatest() {},
    scheduleChatConversationRefresh: (...args) => scheduled.push(args) });
  vm.runInContext(source.slice(source.indexOf("async function loadChatConversation("), source.indexOf("async function createChatConversation(")), context);
  return { state, scheduled, load: context.loadChatConversation };
}

test("opening a past chat clears another thread's spinner and displays its saved messages", async () => {
  const f = historyFixture(async () => ({ conversation: { conversation_id: "past" }, messages: [{ content_markdown: "Saved answer" }], turn_state: { status: "completed" } }));
  await f.load("past", { preserveDraft: true });
  assert.equal(f.state.chatBusy, false);
  assert.equal(f.state.activeChatTurn, null);
  assert.equal(f.state.chatMessages[0].content_markdown, "Saved answer");
  assert.equal(f.state.chatDraft, "Keep draft");
  assert.equal(f.state.chatConversationLoading, false);
});

test("reopening the same finished thread clears stale busy state; active saved turns resume polling", async () => {
  let active = false;
  const f = historyFixture(async () => ({ messages: [], turn_state: active ? { status: "active", turn_id: "restored" } : { status: "failed" } }));
  await f.load("old");
  assert.equal(f.state.chatBusy, false);
  active = true; await f.load("old");
  assert.equal(f.state.chatBusy, true);
  assert.equal(f.state.activeChatTurn.turn_id, "restored");
  assert.equal(f.scheduled.length, 1);
});

test("a late history fetch cannot repopulate a thread after selection was cleared", async () => {
  let resolve;
  const f = historyFixture(() => new Promise((done) => { resolve = done; }));
  const pending = f.load("past");
  await f.load(null);
  resolve({ messages: [{ content_markdown: "Must not appear" }] });
  await pending;
  assert.equal(f.state.selectedChatId, null);
  assert.equal(f.state.chatMessages.length, 0);
});

test("thread click captures the ID before an asynchronous bootstrap clears currentTarget", async () => {
  const event = { currentTarget: { dataset: { conversationId: "past" } } }, loaded = [];
  const context = vm.createContext({ event, state: { appView: "system", chatBusy: false }, isWebShell: true,
    ensureChatReady: async () => { event.currentTarget = null; }, formatError: String,
    loadChatConversation: async (id) => loaded.push(id) });
  const branch = source.slice(source.indexOf('} else if (action === "open-chat-conversation"'), source.indexOf('} else if (action === "start-rename-chat-conversation"'));
  vm.runInContext("async function click() {" + branch.slice(branch.indexOf("{") + 1) + "}", context);
  await context.click();
  assert.deepEqual(loaded, ["past"]);
  assert.equal(context.state.chatBootstrapState, "idle");
});

function fixture(invoke) {
  const retries = [];
  const state = {
    selectedChatId: "session", activeChatTurn: { turn_id: "turn" }, chatBusy: true,
    chatMessages: [], chatTurnEvents: [], chatConversations: [], chatModelQuery: "model",
    pendingChatUserMessage: { role: "user", turn_id: "turn" },
  };
  const context = vm.createContext({
    state, invoke, normalizeChatTurnEvent: (event) => event,
    chatConversationRefreshInFlight: false, chatConversationRefreshTarget: null,
    chatConversationRefreshTerminal: false, chatConversationRefreshDelay: 750,
    clearChatConversationRefreshTimer() {},
    formatError: String,
    scheduleChatConversationRefresh: (...args) => retries.push(args),
    syncSelectedProviderAndModel() {}, queueChatScrollToLatest() {}, scheduleChatRender() {},
  });
  vm.runInContext(refreshSource, context);
  return { state, retries, refresh: context.refreshSelectedChatConversation };
}

test("partial assistant output is displayed while thinking continues", async () => {
  const f = fixture(async () => ({ messages: [{ role: "assistant", turn_id: "turn", content_markdown: "Working", finished: false }], turn_events: [] }));
  await f.refresh("session");
  assert.equal(f.state.chatMessages[0].content_markdown, "Working");
  assert.equal(f.state.chatBusy, true);
  assert.equal(f.retries.length, 1);
});

test("a failed final fetch retains the turn and retries", async () => {
  const f = fixture(async () => { throw new Error("temporary disconnect"); });
  await f.refresh("session", { terminal: true });
  assert.equal(f.state.chatBusy, true);
  assert.equal(f.state.activeChatTurn.turn_id, "turn");
  assert.equal(f.retries.length, 1);
});

test("persisted final response ends thinking without another user message", async () => {
  const f = fixture(async () => ({ messages: [{ role: "assistant", turn_id: "turn", content_markdown: "Done", finished: true }], turn_events: [{ conversation_id: "session", turn_id: "turn", event_type: "turn.completed" }] }));
  await f.refresh("session", { terminal: true });
  assert.equal(f.state.chatMessages[0].content_markdown, "Done");
  assert.equal(f.state.chatBusy, false);
  assert.equal(f.state.activeChatTurn, null);
});

test("completion event cannot stop thinking while the snapshot is still partial", async () => {
  const f = fixture(async () => ({ messages: [{ role: "assistant", message_id: "reply", turn_id: "turn", content_markdown: "Partial", finished: false }],
    turn_events: [{ conversation_id: "session", turn_id: "turn", message_id: "reply", event_type: "turn.completed" }] }));
  await f.refresh("session", { terminal: true });
  assert.equal(f.state.chatBusy, true);
  assert.equal(f.state.chatMessages[0].content_markdown, "Partial");
  assert.equal(f.retries.length, 1);
});

test("a finished older assistant step cannot replace the expected final message", async () => {
  const f = fixture(async () => ({ messages: [{ role: "assistant", message_id: "older", turn_id: "turn", content_markdown: "Earlier", finished: true }],
    turn_events: [{ conversation_id: "session", turn_id: "turn", message_id: "final", event_type: "turn.completed" }] }));
  await f.refresh("session", { terminal: true });
  assert.equal(f.state.chatBusy, true);
  assert.equal(f.retries.length, 1);
});

test("persisted failure ends thinking without an assistant answer and exposes a safe error", async () => {
  const f = fixture(async () => ({ messages: [], turn_events: [
    { conversation_id: "session", turn_id: "turn", event_type: "turn.failed", payload: { error: "Engine stopped. Reconnect." } }] }));
  await f.refresh("session");
  assert.equal(f.state.chatBusy, false);
  assert.equal(f.state.error, "Engine stopped. Reconnect.");
  assert.equal(f.retries.length, 0);
});

test("late completion notifications cannot reopen a completed turn", () => {
  const state = { selectedChatId: "session", chatBusy: false, activeChatTurn: null };
  const context = vm.createContext({ state, normalizeChatTurnEvent: (event) => event, shouldTrackChatTurnEvent: () => true });
  vm.runInContext(source.slice(source.indexOf("function handleChatTurnEvent("), source.indexOf("function normalizeChatTurnEvent(")), context);
  context.handleChatTurnEvent({ conversation_id: "session", turn_id: "session", event_type: "turn.completed" });
  assert.equal(state.chatBusy, false);
  assert.equal(state.activeChatTurn, null);
});

test("notifications for an older turn cannot replace the current turn", () => {
  const turn = { turn_id: "new-turn", events: [] };
  const state = { selectedChatId: "session", chatBusy: true, activeChatTurn: turn };
  const context = vm.createContext({ state, normalizeChatTurnEvent: (event) => event, shouldTrackChatTurnEvent: () => true });
  vm.runInContext(source.slice(source.indexOf("function handleChatTurnEvent("), source.indexOf("function normalizeChatTurnEvent(")), context);
  context.handleChatTurnEvent({ conversation_id: "session", turn_id: "old-turn", event_type: "turn.completed" });
  assert.equal(state.activeChatTurn, turn);
  assert.equal(turn.events.length, 0);
});

test("progress bursts coalesce, slow snapshots never overlap, and completion gets priority", async () => {
  const timers = new Map(), requests = [];
  let timerId = 0;
  const state = { selectedChatId: "session", activeChatTurn: { turn_id: "turn", events: [] }, chatBusy: true,
    chatMessages: [], chatTurnEvents: [], chatConversations: [], chatModelQuery: "model" };
  const context = vm.createContext({
    state, chatConversationRefreshTimer: null, chatConversationRefreshTarget: null,
    chatConversationRefreshTerminal: false, chatConversationRefreshDelay: 750, chatConversationRefreshInFlight: false,
    window: {
      setTimeout(callback, delay) { timers.set(++timerId, { callback, delay }); return timerId; },
      clearTimeout(id) { timers.delete(id); },
    },
    invoke() { return new Promise((resolve) => requests.push(resolve)); },
    normalizeChatTurnEvent: (event) => event, shouldTrackChatTurnEvent: () => true,
    formatError: String, applyActiveTurnEvent() {}, syncSelectedProviderAndModel() {},
    queueChatScrollToLatest() {}, scheduleChatRender() {},
  });
  vm.runInContext(source.slice(source.indexOf("function scheduleChatConversationRefresh("), source.indexOf("function normalizeChatTurnEvent(")), context);
  const progress = () => context.handleChatTurnEvent({ conversation_id: "session", turn_id: "turn", event_type: "turn.progress" });
  const fire = () => { const [id, timer] = timers.entries().next().value; timers.delete(id); timer.callback(); };
  for (let i = 0; i < 200; i++) progress();
  assert.equal(timers.size, 1);
  assert.equal([...timers.values()][0].delay, 750);
  fire();
  assert.equal(requests.length, 1);
  for (let i = 0; i < 200; i++) progress();
  await context.refreshSelectedChatConversation("session");
  assert.equal(requests.length, 1, "No snapshot overlaps the pending fetch");
  assert.equal(timers.size, 0);
  context.handleChatTurnEvent({ conversation_id: "session", turn_id: "turn", event_type: "turn.completed", payload: {} });
  assert.equal(timers.size, 0, "Completion waits for the in-flight fetch, not another concurrent request");
  requests[0]({ messages: [{ role: "assistant", turn_id: "turn", content_markdown: "Partial", finished: false }], turn_events: [] });
  await new Promise(setImmediate);
  assert.equal(state.chatMessages[0].content_markdown, "Partial");
  assert.equal(state.chatBusy, true);
  assert.equal(timers.size, 1);
  assert.equal([...timers.values()][0].delay, 250, "Completion outranks ordinary progress and fallback polling");
  assert.equal(state.activeChatTurn.events.length, 1, "Progress never becomes activity");
  fire();
  requests[1]({ messages: [{ role: "assistant", turn_id: "turn", content_markdown: "Done", finished: true }],
    turn_events: [{ event_type: "turn.completed", turn_id: "turn" }] });
  await new Promise(setImmediate);
  assert.equal(state.chatMessages[0].content_markdown, "Done");
  assert.equal(state.chatBusy, false);
  assert.equal(timers.size, 0, "No unnecessary fetch remains after the final answer");
});

test("timeline removes progress and duplicate completions before the display limit", () => {
  const state = { chatTurnEvents: [
    { event_id: "start", turn_id: "turn", event_type: "turn.started" },
    ...Array.from({ length: 500 }, (_, index) => ({ event_id: "progress" + index, turn_id: "turn", event_type: "turn.progress" })),
    { event_id: "live-terminal", turn_id: "turn", event_type: "turn.completed" },
    { event_id: "message-terminal", turn_id: "turn", event_type: "turn.completed" },
    { event_id: "stored-terminal", turn_id: "turn", event_type: "turn.completed" },
  ] };
  const context = vm.createContext({ state, inlineActivityEventTypes: () => ["turn.started", "turn.progress", "turn.completed"] });
  vm.runInContext(source.slice(source.indexOf("function activityTimelineEvents("), source.indexOf("function selectedConversationAllowsAlways(")), context);
  const events = context.visibleChatTurnEvents();
  assert.equal(events.length, 2);
  assert.equal(events[0].event_type, "turn.started");
  assert.equal(events[1].event_type, "turn.completed");
});

test("tool counts represent actual calls, not approvals or lifecycle updates", () => {
  const context = vm.createContext({});
  vm.runInContext(source.slice(source.indexOf("function countActivityToolCalls("), source.indexOf("function visibleChatTurnEvents(")), context);
  const event = (type, call) => ({ conversation_id: "session", turn_id: "turn", message_id: "reply", event_type: type,
    payload: { tool_name: "gleskit_workspace_status", ...(call ? { call_id: call } : {}) } });
  assert.equal(context.countActivityToolCalls([
    event("tool.approval_requested", "first"), event("tool.approved", "first"),
    event("tool.started", "first"), event("tool.finished", "first"), event("tool.finished", "first"),
  ]), 1);
  assert.equal(context.countActivityToolCalls([
    event("tool.started", "first"), event("tool.finished", "first"), event("tool.started", "second"), event("tool.failed", "second"),
  ]), 2, "Distinct calls to the same tool remain distinct");
  assert.equal(context.countActivityToolCalls([event("tool.started"), event("tool.finished")]), 1, "Legacy start/end pairs count once");
  assert.equal(context.countActivityToolCalls([event("tool.approval_requested"), event("tool.rejected")]), 0, "Rejected permissions did not execute a tool");
});
