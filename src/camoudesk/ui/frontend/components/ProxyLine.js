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

// Chips summarising what was read: type, address, user, password, provider.
export function ProxySummary({ type, host, port, username, hasPassword }) {
  if (!host) return null;
  return html`<div class="parsed">
    <span class="pill">${String(type || "http").toUpperCase()}</span>
    <span class="mono parsed-addr">${host}${port ? `:${port}` : ""}</span>
    ${username ? html`<span class="parsed-item"><span class="muted">user</span> <span class="mono">${username.length > 18 ? `${username.slice(0, 16)}…` : username}</span></span>` : null}
    ${hasPassword ? html`<span class="parsed-item text-ok"><${Icon} name="check" size=${12} /> password</span>` : null}
    ${isEvomiHost(host) ? html`<span class="pill pill-provider">Evomi</span>` : null}
  </div>`;
}

const meaningful = (text) => text.split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith("#"));

/**
 * initialText: starting text (edit forms); it is not parsed until the user changes it.
 * onParts(parts): a single proxy was read (bridge proxy_parse result).
 * onMulti(preview | null, text): several lines were pasted (only when the caller handles imports).
 * onSubmit(): Enter pressed (Shift+Enter adds a line when multi-line is allowed).
 */
export function ProxyLineInput({ initialText = "", scheme = "http", onParts, onMulti, onSubmit, autofocus, disabled, hint }) {
  const [text, setText] = useState(initialText);
  const [touched, setTouched] = useState(false);
  const [status, setStatus] = useState({ kind: "idle" });
  const ref = useRef();

  useEffect(() => {
    if (autofocus && ref.current) setTimeout(() => ref.current && ref.current.focus(), 30);
  }, []);

  useEffect(() => {
    if (!touched) return;
    const lines = meaningful(text);
    if (!lines.length) {
      setStatus({ kind: "idle" });
      onMulti && onMulti(null);
      return;
    }
    const timer = setTimeout(async () => {
      if (lines.length > 1) {
        if (!onMulti) return setStatus({ kind: "error", error: "Paste one proxy here" });
        try {
          const preview = await call("proxies_import_preview", text, scheme);
          setStatus({ kind: "multi", preview });
          onMulti(preview, text);
        } catch (err) {
          setStatus({ kind: "error", error: err.message });
        }
        return;
      }
      onMulti && onMulti(null);
      try {
        const parts = await call("proxy_parse", lines[0], scheme);
        setStatus({ kind: "ok" });
        onParts(parts);
      } catch (err) {
        setStatus({ kind: "error", error: err.message });
      }
    }, 200);
    return () => clearTimeout(timer);
  }, [text, touched]);

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
      onKeyDown=${onKeyDown} onInput=${(e) => { setText(e.currentTarget.value); setTouched(true); }} />
    ${message}
  </div>`;
}
