import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
const source = readFileSync(new URL("./main.js", import.meta.url), "utf8");

test("web shell is visible before backend metadata resolves", async () => {
  const state = { startup: {visible:false}, paths:null, sessions:[], runtime:{} };
  let resolve; const pending = new Promise((done) => { resolve = done; });
  let paints = 0;
  const context = vm.createContext({ state, invoke: () => pending, render: () => { paints++; },
    hydrateWorkspaceState() {}, normalizeRuntimeSession() {}, updateStartupStage() {},
    writeHeartbeat: async () => {}, startHeartbeat() {}, finalizeStartup() {}, warmBackgroundServices() {},
    STARTUP_STAGE_PROGRESS:{}, formatError:String, logStartupWarning() {} });
  vm.runInContext(source.slice(source.indexOf("async function bootstrap()"), source.indexOf("/**\n * Serial background warm-up")), context);
  const result = context.bootstrap();
  assert.equal(paints, 1); assert.equal(state.startup.visible, false);
  assert.equal(state.workspaceBootstrapState, "loading");
  resolve({}); await result; assert.equal(state.workspaceBootstrapState, "ready");
});

test("standalone warm-up does not start chat or scientific imports automatically", async () => {
  let starts = 0;
  const context = vm.createContext({state:{paths:{standalone:true}}, refreshReferenceStatus:async()=>{},
    ensureChatReady:async()=>{starts++;},logStartupWarning(){},formatError:String});
  vm.runInContext(source.slice(source.indexOf("async function warmBackgroundServices("), source.indexOf("function startHeartbeat(")), context);
  await context.warmBackgroundServices(); assert.equal(starts,0);
});

test("Retry Chat explicitly reconnects exactly once before bootstrapping", async () => {
  const calls = []; let complete;
  const done = new Promise((resolve) => { complete = resolve; });
  const context = vm.createContext({state:{paths:{standalone:true}},render(){},formatError:String,
    invoke:async(command)=>{calls.push(command);},ensureChatReady:async()=>{calls.push("bootstrap");complete();}});
  vm.runInContext(source.slice(source.indexOf("function retryChatBootstrap("),source.indexOf("function isChatReady(")),context);
  context.retryChatBootstrap(); await done;
  assert.deepEqual(calls,["reconnect_chat_runtime","bootstrap"]);
});
