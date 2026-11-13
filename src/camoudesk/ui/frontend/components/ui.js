// Shared UI primitives.
import { html, render, useEffect, useLayoutEffect, useRef, useState } from "../vendor/preact-htm.module.js";
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

// Renders children into a node on <body>, outside every scroll/overflow container (the vendored
// Preact has no createPortal). Events inside do not bubble through the component tree.
export function Portal({ children }) {
  const host = useRef(null);
  if (!host.current) host.current = document.createElement("div");
  useLayoutEffect(() => {
    const el = host.current;
    document.body.appendChild(el);
    return () => { render(null, el); el.remove(); };
  }, []);
  useLayoutEffect(() => { render(children, host.current); });
  return null;
}

const MENU_GAP = 4;
const MENU_MARGIN = 8;
// The page runs under a transparent 32px title bar (ui/window_chrome.py) that takes the clicks there.
const MENU_TOP_INSET = 40;

// Place a popover of size (w, h) next to the trigger rect: below and right-aligned by default,
// above when `up` is preferred or there is more room there, always inside the viewport.
export function placeMenu(rect, w, h, up, vw = window.innerWidth, vh = window.innerHeight) {
  const below = vh - rect.bottom - MENU_GAP - MENU_MARGIN;
  const above = rect.top - MENU_GAP - MENU_TOP_INSET;
  const goUp = up ? !(h > above && below > above) : h > below && above > below;
  const maxHeight = Math.max(80, goUp ? above : below);
  const height = Math.min(h, maxHeight);
  const top = goUp ? rect.top - MENU_GAP - height : rect.bottom + MENU_GAP;
  const left = Math.min(Math.max(MENU_MARGIN, rect.right - w), vw - w - MENU_MARGIN);
  return { top: Math.round(top), left: Math.round(Math.max(MENU_MARGIN, left)), maxHeight: Math.floor(maxHeight) };
}

export function Menu({ items, label = "More actions", trigger, up = false, title }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);
  const ref = useRef();
  const pop = useRef();
  const close = () => { setOpen(false); setPos(null); };

  useLayoutEffect(() => {
    if (!open || !ref.current || !pop.current) return;
    const r = ref.current.getBoundingClientRect();
    setPos(placeMenu(r, pop.current.offsetWidth, pop.current.scrollHeight, up));
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const inside = (t) => (ref.current && ref.current.contains(t)) || (pop.current && pop.current.contains(t));
    const onDoc = (e) => !inside(e.target) && close();
    const onKey = (e) => e.key === "Escape" && close();
    // The popover is fixed to the viewport: close it rather than leave it behind on scroll/resize.
    const onScroll = (e) => !(pop.current && pop.current.contains(e.target)) && close();
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const toggle = (e) => { e.stopPropagation(); open ? close() : setOpen(true); };
  const style = pos
    ? `top:${pos.top}px;left:${pos.left}px;max-height:${pos.maxHeight}px`
    : "top:0;left:0;visibility:hidden";
  return html`<div class="menu" ref=${ref}>
    ${trigger ? trigger(toggle) : html`<${IconButton} icon="more" title=${label} onClick=${toggle} />`}
    ${open
      ? html`<${Portal}>
          <div class="menu-pop" role="menu" ref=${pop} style=${style}>
            ${title ? html`<div class="menu-title">${title}</div>` : null}
            ${items.filter(Boolean).map((it) =>
              it === "-"
                ? html`<div class="menu-sep" />`
                : html`<button type="button" role="menuitem" class=${it.danger ? "is-danger" : ""} disabled=${it.disabled}
                    onClick=${(e) => { e.stopPropagation(); close(); it.onClick(); }}>
                    <${Icon} name=${it.icon} />${it.label}</button>`
            )}
          </div>
        <//>`
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
  const inputRef = useRef();
  const [text, setText] = useState("");
  useEffect(() => {
    if (!dialog) return;
    setText(dialog.input ? dialog.input.value : "");
    // Prompts focus their field; destructive dialogs focus Cancel so Enter never deletes by accident.
    setTimeout(() => (dialog.input ? inputRef : dialog.danger ? cancelRef : okRef).current?.focus(), 20);
    const onKey = (e) => {
      if (e.key === "Escape") close(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [dialog]);
  if (!dialog) return null;
  const close = (result) => {
    setState({ dialog: null });
    if (dialog.input) dialog.resolve(result ? text.trim() : null);
    else dialog.resolve(result);
  };
  const canConfirm = !dialog.input || dialog.input.allowEmpty || text.trim();
  return html`<div class="overlay overlay-dialog" onMouseDown=${(e) => e.target === e.currentTarget && close(false)}>
    <div class="dialog" role="alertdialog" aria-modal="true" aria-labelledby="dlg-title">
      <h3 id="dlg-title">${dialog.title}</h3>
      ${dialog.message ? html`<p>${dialog.message}</p>` : null}
      ${dialog.input
        ? html`<label class="field dialog-field"><span class="field-label">${dialog.input.label}</span>
            <input ref=${inputRef} class="input" value=${text} placeholder=${dialog.input.placeholder}
              onInput=${(e) => setText(e.currentTarget.value)} onKeyDown=${(e) => e.key === "Enter" && canConfirm && close(true)} /></label>`
        : null}
      <div class="dialog-actions">
        <button ref=${cancelRef} class="btn btn-secondary" onClick=${() => close(false)}><span>Cancel</span></button>
        <button ref=${okRef} disabled=${!canConfirm} class=${`btn ${dialog.danger ? "btn-danger" : "btn-primary"}`} onClick=${() => close(true)}>
          <span>${dialog.confirmLabel}</span></button>
      </div>
    </div>
  </div>`;
}
