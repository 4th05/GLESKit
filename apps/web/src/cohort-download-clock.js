export function formatElapsed(seconds) {
  const total = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const parts = [Math.floor(total / 60) % 60, total % 60].map((part) => String(part).padStart(2, "0"));
  if (total >= 3600) parts.unshift(String(Math.floor(total / 3600)).padStart(2, "0"));
  return parts.join(":");
}

export function elapsedDownloadTime(job, startedAt = 0, finishedAt = 0, now = Date.now()) {
  const parse = (value) => value ? (typeof value === "number" ? value : Date.parse(value)) : NaN;
  const start = parse(job?.started_at);
  const end = parse(job?.finished_at);
  const terminal = ["completed", "failed", "cancelled"].includes(String(job?.status || "").toLowerCase());
  const localStart = parse(startedAt);
  const first = Number.isFinite(start) ? (Number.isFinite(localStart) ? Math.min(start, localStart) : start) : localStart;
  const last = terminal ? (Number.isFinite(end) ? end : finishedAt || parse(job?.updated_at)) : now;
  return formatElapsed(Number.isFinite(first) && Number.isFinite(last) ? (last - first) / 1000 : 0);
}

// The elapsed display ticks independently of job polling. It performs no API
// calls or page renders and calculates from timestamps rather than tick counts.
export function createCohortDownloadClock({ doc = document, timers = window, now = Date.now } = {}) {
  let interval = null, snapshot = null;
  const paint = () => {
    if (!snapshot) return;
    const text = elapsedDownloadTime(snapshot.job, snapshot.startedAt, snapshot.finishedAt, now());
    for (const node of doc.querySelectorAll("[data-cohort-elapsed]")) node.textContent = text;
  };
  const stop = () => {
    if (interval !== null) timers.clearInterval(interval);
    interval = null;
  };
  return {
    update(value) {
      snapshot = value; paint();
      if (!value.running) stop();
      else if (interval === null) interval = timers.setInterval(paint, 1000);
    },
    stop,
  };
}
