// Notifications live outside #app so streaming renders cannot close a dialog,
// reset its scroll position or steal its focus. Native showModal makes the
// background inert and provides keyboard focus containment.
export class NoticeQueue {
  constructor(changed = () => {}) { this.changed = changed; this.seen = new Map(); this.pending = []; this.active = null; }
  push(entry) {
    if (!entry?.message) return;
    const severity = ["positive", "follow-up", "followup"].includes(entry.severity) ? "success"
      : ["error", "warning", "success"].includes(entry.severity) ? entry.severity : "info";
    const notice = { ...entry, message: String(entry.message), severity };
    if ([this.active, ...this.pending].some((row) => row?.message === notice.message)) return;
    this.pending.push(notice); this.next();
  }
  update(group, entries) {
    const current = new Set();
    for (const entry of entries) {
      const key = group + ":" + entry.source;
      if (!entry.message) continue;
      current.add(key);
      const fingerprint = entry.severity + ":" + entry.message;
      if (this.seen.get(key) !== fingerprint) { this.seen.set(key, fingerprint); this.push(entry); }
    }
    for (const key of this.seen.keys()) if (key.startsWith(group + ":") && !current.has(key)) this.seen.delete(key);
  }
  next() { if (!this.active && this.pending.length) { this.active = this.pending.shift(); this.changed(this.active); } }
  close() { const closed = this.active; this.active = null; closed?.acknowledge?.(); this.next(); }
}

let queue;
export function initializeNotifications(doc = document) {
  if (queue) return queue;
  let dialog;
  queue = new NoticeQueue((entry) => {
    if (!dialog) {
      dialog = doc.createElement("dialog");
      dialog.className = "gleskit-notification";
      dialog.setAttribute("role", "alertdialog");
      dialog.setAttribute("aria-modal", "true");
      dialog.setAttribute("aria-labelledby", "gleskit-notification-title");
      dialog.setAttribute("aria-describedby", "gleskit-notification-body");
      dialog.innerHTML = `<header><h2 id="gleskit-notification-title"></h2></header><div id="gleskit-notification-body" tabindex="0"></div><footer><button type="button" class="action-button primary compact" autofocus>Close</button></footer>`;
      dialog.querySelector("button").addEventListener("click", () => dialog.close());
      // Escape is an explicit keyboard acknowledgment, equivalent to Close.
      dialog.addEventListener("close", () => {
        queue.close();
        if (!queue.active && (doc.activeElement === doc.body || !doc.activeElement?.isConnected)) {
          (doc.querySelector("#web-settings-dialog[open] #web-settings-close") || doc.querySelector("#chat-draft:not(:disabled), #app button"))?.focus();
        }
      });
      doc.body.append(dialog);
    }
    dialog.dataset.severity = entry.severity;
    dialog.querySelector("h2").textContent = entry.title || ({ error: "Error", warning: "Warning", success: "Done", info: "Information" }[entry.severity]);
    const body = dialog.querySelector("#gleskit-notification-body");
    body.textContent = entry.message;
    body.scrollTop = 0;
    dialog.showModal();
    dialog.querySelector("button").focus();
  });
  doc.addEventListener("gleskit:notice", (event) => queue.push(event.detail));
  doc.addEventListener("gleskit:notices", (event) => queue.update(event.detail.group, event.detail.entries));
  return queue;
}

export function syncNotifications(entries) { initializeNotifications().update("main", entries); }
