// Shared UI primitives.
import { html, useEffect, useRef, useState } from "../vendor/preact-htm.module.js";
import { setState, useStore } from "../lib/store.js";
import { Icon } from "./icons.js";

export function Button({ kind = "secondary", size = "", icon, busy, disabled, onClick, children, title, type = "button" }) {
  return html`<button type=${type} class=${`btn btn-${kind} ${size ? "btn-" + size : ""}`} disabled=${disabled || busy}
    onClick=${onClick} title=${title} aria-busy=${busy ? "true" : undefined}>
    ${busy ? html`<span class="spinner" />` : icon ? html`<${Icon} name=${icon} />` : null}
    ${children ? html`<span>${children}</span>` : null}
  </button>`;
}

export function IconButton({ icon, title, onClick, disabled, kind = "ghost" }) {
  return html`<button type="button" class=${`icon-btn icon-btn-${kind}`} title=${title} aria-label=${title}
    disabled=${disabled} onClick=${onClick}><${Icon} name=${icon} /></button>`;
}

export function Field({ label, hint, error, children, wide }) {
  return html`<label class=${`field ${wide ? "field-wide" : ""}`}>
    <span class="field-label">${label}</span>
    ${children}
    ${error ? html`<span class="field-error">${error}</span>` : hint ? html`<span class="field-hint">${hint}</span>` : null}
  </label>`;
}

export function TextInput({ value, onInput, placeholder, type = "text", list, disabled, autofocus, mono, inputRef }) {
  const local = useRef();
  const ref = inputRef || local;
  useEffect(() => {
    if (autofocus && ref.current) setTimeout(() => ref.current && ref.current.focus(), 30);
  }, []);
  return html`<input ref=${ref} class=${`input ${mono ? "mono" : ""}`} type=${type} value=${value ?? ""} placeholder=${placeholder}
    list=${list} disabled=${disabled} spellcheck="false" autocomplete="off"
    onInput=${(e) => onInput && onInput(e.currentTarget.value)} />`;
}

export function TextArea({ value, onInput, placeholder, rows = 3, disabled, mono }) {
  return html`<textarea class=${`input textarea ${mono ? "mono" : ""}`} rows=${rows} value=${value ?? ""} placeholder=${placeholder}
    disabled=${disabled} spellcheck="false" onInput=${(e) => onInput && onInput(e.currentTarget.value)} />`;
}

export function Select({ value, onChange, options, disabled }) {
  return html`<div class="select-wrap">
    <select class="input select" value=${value} disabled=${disabled} onChange=${(e) => onChange(e.currentTarget.value)}>
      ${options.map((o) => html`<option value=${o.value}>${o.label}</option>`)}
    </select>
    <${Icon} name="chevron" class="select-caret" />
  </div>`;
}

export function Toggle({ checked, onChange, label, hint, disabled }) {
  return html`<label class=${`toggle ${disabled ? "is-disabled" : ""}`}>
    <input type="checkbox" checked=${checked} disabled=${disabled} onChange=${(e) => onChange(e.currentTarget.checked)} />
    <span class="toggle-track"><span class="toggle-thumb" /></span>
    <span class="toggle-text"><span>${label}</span>${hint ? html`<small>${hint}</small>` : null}</span>
  </label>`;
}

export function Segmented({ value, onChange, options, disabled }) {
  return html`<div class="segmented" role="radiogroup">
    ${options.map(
      (o) => html`<button type="button" role="radio" aria-checked=${value === o.value} disabled=${disabled}
        class=${value === o.value ? "is-active" : ""} onClick=${() => onChange(o.value)}>
        ${o.icon ? html`<${Icon} name=${o.icon} />` : null}<span>${o.label}</span></button>`
    )}
  </div>`;
}

export function Tabs({ value, onChange, tabs }) {
  return html`<div class="tabs" role="tablist">
    ${tabs.map(
      (t) => html`<button type="button" role="tab" aria-selected=${value === t.value}
        class=${value === t.value ? "is-active" : ""} onClick=${() => onChange(t.value)}>
        <${Icon} name=${t.icon} /> ${t.label}</button>`
    )}
  </div>`;
}

const STATUS = {
  running: ["Running", "ok"],
  starting: ["Starting", "busy"],
  stopping: ["Stopping", "busy"],
  busy: ["Working", "busy"],
  stopped: ["Stopped", "idle"],
};

export function StatusChip({ status, error }) {
  if (status === "stopped" && error) return html`<span class="chip chip-err" title=${error}><span class="dot" />Error</span>`;
  const [label, kind] = STATUS[status] || [status, "idle"];
  return html`<span class=${`chip chip-${kind}`}><span class="dot" />${label}</span>`;
}

export function CountryChip({ code }) {
  return code ? html`<span class="cc">${code}</span>` : null;
}

export function Menu({ items, label = "More actions", trigger, up = false, title }) {
  const [open, setOpen] = useState(false);
  const ref = useRef();
  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => ref.current && !ref.current.contains(e.target) && setOpen(false);
    const onKey = (e) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const toggle = (e) => { e.stopPropagation(); setOpen(!open); };
  return html`<div class=${`menu ${up ? "menu-up" : ""}`} ref=${ref}>
    ${trigger ? trigger(toggle) : html`<${IconButton} icon="more" title=${label} onClick=${toggle} />`}
    ${open
      ? html`<div class="menu-pop" role="menu">
          ${title ? html`<div class="menu-title">${title}</div>` : null}
          ${items.filter(Boolean).map((it) =>
            it === "-"
              ? html`<div class="menu-sep" />`
              : html`<button type="button" role="menuitem" class=${it.danger ? "is-danger" : ""} disabled=${it.disabled}
                  onClick=${(e) => { e.stopPropagation(); setOpen(false); it.onClick(); }}>
                  <${Icon} name=${it.icon} />${it.label}</button>`
          )}
        </div>`
      : null}
  </div>`;
}

export function Empty({ icon = "profiles", title, children, action }) {
  return html`<div class="empty">
    <div class="empty-icon"><${Icon} name=${icon} size=${22} /></div>
    <h3>${title}</h3>
    ${children ? html`<p>${children}</p>` : null}
    ${action || null}
  </div>`;
}

export function Toasts() {
  const toasts = useStore((s) => s.toasts);
  return html`<div class="toasts" aria-live="polite">
    ${toasts.map(
      (t) => html`<div key=${t.id} class=${`toast toast-${t.kind}`}>
        <${Icon} name=${t.kind === "error" ? "alert" : t.kind === "success" ? "check" : "info"} />
        <span>${t.message}</span>
        <button class="toast-x" aria-label="Dismiss" onClick=${() => setState((s) => ({ toasts: s.toasts.filter((x) => x.id !== t.id) }))}>
          <${Icon} name="x" size=${14} /></button>
      </div>`
    )}
  </div>`;
}

export function DialogHost() {
  const dialog = useStore((s) => s.dialog);
  const okRef = useRef();
  const cancelRef = useRef();
  useEffect(() => {
    if (!dialog) return;
    // Destructive dialogs focus Cancel so Enter never deletes by accident.
    setTimeout(() => (dialog.danger ? cancelRef : okRef).current?.focus(), 20);
    const onKey = (e) => {
      if (e.key === "Escape") close(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dialog]);
  if (!dialog) return null;
  const close = (result) => {
    setState({ dialog: null });
    dialog.resolve(result);
  };
  return html`<div class="overlay overlay-dialog" onMouseDown=${(e) => e.target === e.currentTarget && close(false)}>
    <div class="dialog" role="alertdialog" aria-modal="true" aria-labelledby="dlg-title">
      <h3 id="dlg-title">${dialog.title}</h3>
      <p>${dialog.message}</p>
      <div class="dialog-actions">
        <button ref=${cancelRef} class="btn btn-secondary" onClick=${() => close(false)}><span>Cancel</span></button>
        <button ref=${okRef} class=${`btn ${dialog.danger ? "btn-danger" : "btn-primary"}`} onClick=${() => close(true)}>
          <span>${dialog.confirmLabel}</span></button>
      </div>
    </div>
  </div>`;
}
