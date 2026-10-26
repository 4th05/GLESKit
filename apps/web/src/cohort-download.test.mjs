import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { createCohortDownloadClock, elapsedDownloadTime, formatElapsed } from "./cohort-download-clock.js";

test("elapsed time uses real timestamps, includes submission time, and freezes on every terminal state", () => {
  for (const [seconds, expected] of [[0, "00:00"], [65, "01:05"], [3601, "01:00:01"], [-1, "00:00"], [NaN, "00:00"]]) assert.equal(formatElapsed(seconds), expected);
  assert.equal(elapsedDownloadTime({ status: "running", started_at: 2000 }, 1000, 0, 66000), "01:05");
  for (const status of ["completed", "failed", "cancelled"]) {
    assert.equal(elapsedDownloadTime({ status, started_at: 1000, finished_at: 66000 }, 0, 0, 900000), "01:05");
    assert.equal(elapsedDownloadTime({ status, started_at: 1000 }, 0, 66000, 900000), "01:05");
  }
  assert.equal(elapsedDownloadTime({ status: "running", started_at: "invalid" }, 1000, 0, 4000), "00:03");
});

test("one DOM-only timer advances without progress events, renders or API calls and stops when hidden/finished", () => {
  let at = 1000, nextId = 0;
  const intervals = new Map(), nodes = [{}, {}];
  const clock = createCohortDownloadClock({ now: () => at, doc: { querySelectorAll: () => nodes }, timers: {
    setInterval(callback, delay) { assert.equal(delay, 1000); intervals.set(++nextId, callback); return nextId; },
    clearInterval(id) { intervals.delete(id); },
  } });
  const snapshot = { job: { status: "running", started_at: 1000 }, running: true };
  clock.update(snapshot); clock.update(snapshot);
  assert.equal(intervals.size, 1);
  at = 66000; [...intervals.values()][0]();
  assert.ok(nodes.every((node) => node.textContent === "01:05"));
  clock.update({ ...snapshot, running: false }); assert.equal(intervals.size, 0);
  at = 91000; clock.update(snapshot);
  assert.equal(nodes[0].textContent, "01:30", "Returning to the page must not reset the timer");
  clock.update({ job: { status: "completed", started_at: 1000, finished_at: 66000 }, running: false });
  assert.equal(nodes[0].textContent, "01:05"); assert.equal(intervals.size, 0);
});

const source = readFileSync(new URL("./main.js", import.meta.url), "utf8");
const active = (extra = {}) => ({ job_id: "job_fixture", status: "running", stage: "Downloading", title: "Fixture cohort", can_cancel: true,
  started_at: "2026-10-22T10:00:00Z", progress_percent: null, ...extra });
function fixture(handler = async () => active()) {
  const state = { appView: "cohorts", cohortTab: "download", cohortProviders: [{ provider_id: "tcga", name: "TCGA" }],
    cohortCatalog: [{ dataset_id: "TCGA-DLBC", title: "Fixture cohort", sample_count: 48 }], selectedCohortProviderId: "tcga",
    selectedCohortDatasetId: "TCGA-DLBC", selectedCohortTissue: "Blood", cohortDownloadScope: "subset", cohortSubsetMode: "absolute",
    cohortSubsetValueText: "20", cohortDownloadGeneration: 0, cohortDownloadStartedAt: 0, cohortDownloadFinishedAt: 0, cohortDownloadJob: null };
  const intervals = new Map(), calls = [], snapshots = [], clockUpdates = [];
  let intervalId = 0, libraryReads = 0, context;
  context = vm.createContext({ state, isWebShell: true, elapsedDownloadTime, escapeHtml: (value) => String(value ?? "").replaceAll("<", "&lt;"),
    cohortDownloadClock: { update: (value) => clockUpdates.push(value) },
    document: { getElementById: () => null },
    window: { setInterval(callback) { intervals.set(++intervalId, callback); return intervalId; }, clearInterval(id) { intervals.delete(id); } },
    render() { snapshots.push(context.isCohortDownloadLocked()); context.syncCohortDownloadClock(); },
    invoke(command, values) { calls.push({ command, values }); return handler(command, values); }, formatError: String,
    refreshCohorts: async () => { libraryReads++; }, selectCohort: async () => {},
    formatTimestamp: String, renderCohortDownloadJobLog: () => "", getFilteredCohortCatalog: (rows) => rows, getCohortTissueOptions: () => ["Blood"],
    FormData: class { get(name) { return ({ title: "Fixture cohort", provider_id: "tcga", dataset_id: "TCGA-DLBC", tissue: "Blood", download_scope: "subset", subset_mode: "absolute", subset_value: "20" })[name]; } },
  });
  vm.runInContext(source.slice(source.indexOf("function clearCohortDownloadPoll("), source.indexOf("async function selectCohort(")), context);
  vm.runInContext(source.slice(source.indexOf("async function downloadCohort("), source.indexOf("async function createSessionFromCohortId(")), context);
  vm.runInContext(source.slice(source.indexOf("function renderCohortDownloadJobPanel("), source.indexOf("function renderCohortDownloadJobLog(")), context);
  vm.runInContext(source.slice(source.indexOf("function renderCohortProviderPanel("), source.indexOf("function renderChatMain(")), context);
  return { state, context, intervals, calls, snapshots, clockUpdates, get libraryReads() { return libraryReads; } };
}

test("starting/active jobs block only the source controls; overlay Cancel remains outside the inert form", () => {
  const f = fixture();
  for (const status of ["queued", "running"]) {
    f.state.cohortDownloadJob = active({ status });
    const html = f.context.renderCohortProviderPanel();
    assert.match(html, /class="cohort-download-controls" inert/);
    assert.match(html, /aria-label="Download configuration" disabled/);
    assert.ok(html.indexOf("</form>") < html.indexOf('class="cohort-download-overlay"'));
    assert.equal((html.match(/data-action="cancel-cohort-download"/g) || []).length, 1);
    assert.match(html, /role="timer"/);
    assert.match(html, /is-indeterminate/);
    assert.doesNotMatch(html, /aria-valuenow=/, "Unknown progress must not become a fabricated percentage");
  }
  f.state.cohortDownloadStarting = true;
  assert.match(f.context.renderCohortProviderPanel(), /Starting download/);
  f.state.cohortDownloadStarting = false;
  f.state.cohortDownloadJob = active({ progress_percent: 42.5 });
  assert.match(f.context.renderCohortProviderPanel(), /aria-valuenow="42.5"/);
});

test("completion, failure and cancellation all remove the overlay and enable the source fields", async () => {
  for (const status of ["completed", "failed", "cancelled"]) {
    const f = fixture(); f.state.cohortDownloadJob = active(); f.context.startCohortDownloadPolling("job_fixture");
    await f.context.applyCohortDownloadSnapshot(active({ status, can_cancel: false, error: status === "failed" ? "Synthetic failure" : null }));
    assert.equal(f.context.isCohortDownloadLocked(), false);
    assert.equal(f.intervals.size, 0);
    const html = f.context.renderCohortProviderPanel();
    assert.doesNotMatch(html, /class="cohort-download-overlay"|class="cohort-download-controls" inert|aria-label="Download configuration" disabled/);
    assert.equal(f.clockUpdates.at(-1).running, false);
    if (status === "failed") assert.equal(f.state.error, "Synthetic failure");
    if (status === "completed") assert.match(f.state.message, /Downloaded cohort/);
    if (status === "cancelled") assert.equal(f.state.cohortNotice.kind, "warning");
  }
});

test("submission locks immediately, rejects double clicks, and does not pull the user back from another page", async () => {
  let finish;
  const f = fixture(() => new Promise((resolve) => { finish = resolve; }));
  const pending = f.context.downloadCohort({});
  assert.equal(f.context.isCohortDownloadLocked(), true);
  assert.equal(f.clockUpdates.at(-1).running, true);
  await assert.rejects(f.context.downloadCohort({}), /Finish or cancel/);
  f.state.appView = "workspace"; finish(active()); await pending;
  assert.equal(f.calls.length, 1);
  assert.equal(f.state.appView, "workspace");
  assert.equal(f.state.cohortDownloadStarting, false);
  assert.equal(f.intervals.size, 1);
  assert.equal(f.clockUpdates.at(-1).running, false);
});

test("failed submission unlocks immediately and does not start a poller", async () => {
  const f = fixture(async () => { throw new Error("Synthetic submission failure"); });
  await assert.rejects(f.context.downloadCohort({}), /Synthetic submission failure/);
  assert.equal(f.context.isCohortDownloadLocked(), false);
  assert.equal(f.intervals.size, 0);
  assert.equal(f.clockUpdates.at(-1).running, false);
});

test("the elapsed clock pauses in a hidden tab and resumes without changing the job", () => {
  const f = fixture(); f.state.cohortDownloadJob = active();
  f.context.syncCohortDownloadClock(); assert.equal(f.clockUpdates.at(-1).running, true);
  f.context.document.hidden = true;
  f.context.syncCohortDownloadClock(); assert.equal(f.clockUpdates.at(-1).running, false);
  f.context.document.hidden = false;
  f.context.syncCohortDownloadClock(); assert.equal(f.clockUpdates.at(-1).running, true);
  assert.equal(f.state.cohortDownloadJob.job_id, "job_fixture");
  assert.equal(f.calls.length, 0);
});

test("slow snapshot requests never overlap and an old response cannot resurrect a cancelled download", async () => {
  let finish;
  const f = fixture((command) => command === "get_cohort_download_job" ? new Promise((resolve) => { finish = resolve; })
    : Promise.resolve(active({ status: "cancelled", can_cancel: false })));
  f.state.cohortDownloadJob = active();
  const pending = f.context.refreshCohortDownloadJob();
  await f.context.refreshCohortDownloadJob();
  assert.equal(f.calls.length, 1);
  await f.context.cancelCohortDownload(); finish(active()); await pending;
  assert.equal(f.state.cohortDownloadJob.status, "cancelled");
  assert.equal(f.context.isCohortDownloadLocked(), false);
});

test("a status error unlocks editing without claiming the download stopped or allowing a duplicate", async () => {
  let unavailable = true;
  const f = fixture(async () => { if (unavailable) throw new Error("Synthetic status error"); return active({ status: "completed", can_cancel: false }); });
  f.state.cohortDownloadJob = active();
  f.context.startCohortDownloadPolling("job_fixture"); [...f.intervals.values()][0]();
  await new Promise(setImmediate);
  assert.equal(f.context.isCohortDownloadLocked(), false);
  assert.equal(f.state.cohortDownloadJob.status, "running");
  assert.match(f.state.cohortDownloadObservationError, /may still be running/);
  assert.match(f.context.renderCohortProviderPanel(), /retry-cohort-download-status/);
  await assert.rejects(f.context.downloadCohort({}), /Finish or cancel/);
  unavailable = false; await f.context.retryCohortDownloadStatus();
  assert.equal(f.state.cohortDownloadJob.status, "completed");
  assert.ok(f.calls.every((row) => row.command === "get_cohort_download_job"));
});

test("a failed Cancel can be retried and terminal notifications are not duplicated", async () => {
  const f = fixture(async () => { throw new Error("Synthetic cancellation failure"); });
  f.state.cohortDownloadJob = active();
  await assert.rejects(f.context.cancelCohortDownload(), /Synthetic cancellation failure/);
  assert.equal(f.state.cohortDownloadCancelling, false);
  assert.equal(f.context.isCohortDownloadLocked(), false);
  assert.match(f.state.cohortDownloadObservationError, /Cancellation could not be confirmed/);
  const done = active({ status: "completed", can_cancel: false });
  await f.context.applyCohortDownloadSnapshot(done); await f.context.applyCohortDownloadSnapshot(done);
  assert.equal(f.libraryReads, 1);
});

test("even a job already complete in its submission response emits success and leaves no timers", async () => {
  const f = fixture(async () => active({ status: "completed", can_cancel: false }));
  await f.context.downloadCohort({});
  assert.match(f.state.message, /Downloaded cohort/);
  assert.equal(f.context.isCohortDownloadLocked(), false);
  assert.equal(f.intervals.size, 0);
});

test("a late library refresh from a completed request cannot unlock or stop the next download", async () => {
  let calls = 0, releaseLibrary, acceptNext;
  const f = fixture(async () => ++calls === 1 ? active({ status: "completed", can_cancel: false }) : new Promise((resolve) => { acceptNext = resolve; }));
  f.context.refreshCohorts = () => new Promise((resolve) => { releaseLibrary = resolve; });
  const first = f.context.downloadCohort({}); await new Promise(setImmediate);
  assert.equal(f.context.isCohortDownloadLocked(), false);
  const next = f.context.downloadCohort({});
  assert.equal(f.state.cohortDownloadStarting, true);
  releaseLibrary(); await first;
  assert.equal(f.state.cohortDownloadStarting, true, "Old finally must not clear the new submission's lock");
  acceptNext(active({ job_id: "job_second" })); await next;
  assert.equal(f.state.cohortDownloadJob.job_id, "job_second");
  assert.equal(f.intervals.size, 1);
});
