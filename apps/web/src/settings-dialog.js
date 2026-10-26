// Keep Settings outside #app: chat streaming renders must not rebuild its forms.
export function createSettingsDialog({ render, bind, onClose = () => {}, doc = document }) {
  let dialog;
  const refresh = () => {
    if (!dialog?.open) return;
    const previousSection = dialog.querySelector("[data-settings-section]")?.dataset.settingsSection;
    const scroll = dialog.querySelector("#web-settings-content")?.scrollTop || 0;
    const active = doc.activeElement;
    const focus = dialog.contains(active) ? { id: active.id, name: active.name, start: active.selectionStart, end: active.selectionEnd } : null;
    const form = dialog.querySelector("#web-provider-form");
    // A key stays only in its DOM input. Hold it transiently during a same-form
    // redraw; never put it in panel state, storage, HTML or a different endpoint.
    const scope = form?.dataset.credentialScope;
    const key = form?.elements.namedItem("key")?.value || "";
    dialog.innerHTML = render();
    const nextForm = dialog.querySelector("#web-provider-form");
    if (key && scope === nextForm?.dataset.credentialScope) nextForm.elements.namedItem("key").value = key;
    dialog.querySelector("#web-settings-close").addEventListener("click", () => dialog.close());
    bind();
    const sameSection = previousSection === dialog.querySelector("[data-settings-section]")?.dataset.settingsSection;
    dialog.querySelector("#web-settings-content").scrollTop = sameSection ? scroll : 0;
    if (doc.querySelector(".gleskit-notification[open]")) return;
    const target = sameSection && focus ? (focus.id ? doc.getElementById(focus.id)
      : /^[a-zA-Z]+$/.test(focus.name || "") ? dialog.querySelector(`[name="${focus.name}"]`) : null) : null;
    if (target && dialog.contains(target) && !target.disabled) {
      target.focus({ preventScroll: true });
      if (typeof focus.start === "number" && typeof target.setSelectionRange === "function") target.setSelectionRange(focus.start, focus.end);
    } else if (previousSection) dialog.querySelector("#web-settings-section-title").focus({ preventScroll: true });
  };
  return {
    get isOpen() { return Boolean(dialog?.open); },
    refresh,
    open() {
      if (!dialog) {
        dialog = doc.createElement("dialog");
        dialog.id = "web-settings-dialog";
        dialog.className = "web-settings-dialog";
        dialog.setAttribute("aria-modal", "true");
        dialog.setAttribute("aria-labelledby", "web-settings-title");
        dialog.addEventListener("close", () => {
          dialog.replaceChildren(); // Clear unsaved password inputs on Close/Escape.
          onClose();
          if (!doc.querySelector(".gleskit-notification[open]")) doc.getElementById("open-settings-button")?.focus();
        });
        doc.body.append(dialog);
      }
      if (dialog.open) return;
      dialog.showModal();
      refresh();
      dialog.querySelector("#web-settings-close").focus();
    },
    close() { if (dialog?.open) dialog.close(); },
  };
}
