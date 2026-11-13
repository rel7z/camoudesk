// One-line proxy entry: paste any provider format and it is read as you type (no "Fill" step).
// Pasting several lines switches to a multi-proxy import preview when the caller allows it.
import { html, useEffect, useRef, useState } from "../vendor/preact-htm.module.js";
import { call } from "../lib/api.js";
import { isEvomiHost } from "./Evomi.js";
import { Icon } from "./icons.js";

export const FORMATS_HINT = "host:port:user:pass, user:pass@host:port or scheme://user:pass@host:port";

// What a saved proxy looks like without its password (for the edit form's starting text).
export function proxyLineText(p) {
  if (!p || !p.host) return "";
  const host = String(p.host).includes(":") ? `[${p.host}]` : p.host;
  return `${p.type || "http"}://${p.username ? `${p.username}@` : ""}${host}:${p.port}`;
}

// Chips summarising what was read: type, address, user, password (length only), provider.
export function ProxySummary({ type, host, port, username, hasPassword, passwordLength }) {
  if (!host) return null;
  return html`<div class="parsed">
    <span class="pill">${String(type || "http").toUpperCase()}</span>
    <span class="mono parsed-addr">${host}${port ? `:${port}` : ""}</span>
    ${username ? html`<span class="parsed-item"><span class="muted">user</span> <span class="mono">${username.length > 18 ? `${username.slice(0, 16)}…` : username}</span></span>` : null}
    ${hasPassword ? html`<span class="parsed-item text-ok"><${Icon} name="check" size=${12} /> password${passwordLength ? html`<span class="muted"> · ${passwordLength} chars</span>` : " saved"}</span>` : null}
    ${isEvomiHost(host) ? html`<span class="pill pill-provider">Evomi</span>` : null}
  </div>`;
}

const meaningful = (text) => text.split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith("#"));

/**
 * initialText: starting text (edit forms); it is not parsed until the user changes it.
 * onParts(parts): a single proxy was read (bridge proxy_parse result).
 * onMulti(preview | null, text): several lines were pasted (only when the caller handles imports).
 * onSubmit(): Enter pressed (Shift+Enter adds a line when multi-line is allowed).
 * controlRef: gets { flush() }, which reads the text as it is right now (skipping the typing delay)
 *   and resolves to {parts} | {preview, text} | {error} | {untouched} | {empty}. Save must call it, or
 *   it could store what was read before the last keystrokes.
 */
export function ProxyLineInput({ initialText = "", scheme = "http", onParts, onMulti, onSubmit, controlRef, autofocus, disabled, hint }) {
  const [text, setText] = useState(initialText);
  const [touched, setTouched] = useState(false);
  const [status, setStatus] = useState({ kind: "idle" });
  const ref = useRef();
  const seq = useRef(0); // only the newest read may update the form
  const timer = useRef(null);
  const latest = useRef({ text: initialText, touched: false });
  latest.current = { text, touched };

  useEffect(() => {
    if (autofocus && ref.current) setTimeout(() => ref.current && ref.current.focus(), 30);
  }, []);

  const read = async (value) => {
    const id = ++seq.current;
    const stale = () => id !== seq.current;
    const lines = meaningful(value);
    if (!lines.length) {
      setStatus({ kind: "idle" });
      onMulti && onMulti(null);
      return { empty: true };
    }
    if (lines.length > 1) {
      if (!onMulti) {
        setStatus({ kind: "error", error: "Paste one proxy here" });
        return { error: "Paste one proxy here" };
      }
      try {
        const preview = await call("proxies_import_preview", value, scheme);
        if (stale()) return { stale: true };
        setStatus({ kind: "multi", preview });
        onMulti(preview, value);
        return { preview, text: value };
      } catch (err) {
        if (stale()) return { stale: true };
        setStatus({ kind: "error", error: err.message });
        return { error: err.message };
      }
    }
    try {
      const parts = await call("proxy_parse", lines[0], scheme);
      if (stale()) return { stale: true };
      onMulti && onMulti(null);
      setStatus({ kind: "ok" });
      onParts(parts);
      return { parts };
    } catch (err) {
      if (stale()) return { stale: true };
      setStatus({ kind: "error", error: err.message });
      return { error: err.message };
    }
  };

  useEffect(() => {
    if (!touched) return;
    clearTimeout(timer.current);
    timer.current = setTimeout(() => read(text), 200);
    return () => clearTimeout(timer.current);
  }, [text, touched]);

  useEffect(() => {
    if (!controlRef) return;
    controlRef.current = {
      flush: () => {
        clearTimeout(timer.current);
        const { text: value, touched: changed } = latest.current;
        return changed ? read(value) : Promise.resolve({ untouched: true });
      },
    };
    return () => { controlRef.current = null; };
  });

  const rows = Math.min(Math.max(text.split("\n").length, 1), 6);
  const onKeyDown = (e) => {
    if (e.key === "Enter" && !(e.shiftKey && onMulti)) {
      e.preventDefault();
      onSubmit && onSubmit();
    }
  };
  const message = status.kind === "error"
    ? html`<span class="field-error">${/format/i.test(status.error) ? status.error : `${status.error}. Accepted: ${FORMATS_HINT}`}</span>`
    : status.kind === "multi"
    ? html`<span class="field-hint">${status.preview.proxies.length} proxies detected${status.preview.errors.length ? ` · ${status.preview.errors.length} line(s) skipped` : ""}</span>`
    : !text.trim() || !touched
    ? html`<span class="field-hint">${hint || `Paste it as your provider gave it: ${FORMATS_HINT}`}</span>`
    : null;
  return html`<div class="proxy-line">
    <textarea ref=${ref} class="input mono proxy-line-input" rows=${rows} value=${text} disabled=${disabled}
      spellcheck="false" autocomplete="off" placeholder="host:port:user:pass"
      onKeyDown=${onKeyDown} onInput=${(e) => { setText(e.currentTarget.value); setTouched(true); latest.current = { text: e.currentTarget.value, touched: true }; }} />
    ${message}
  </div>`;
}
