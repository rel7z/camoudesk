// Proxies page: saved proxy table, add/edit drawer, bulk import, check all, rotate IP.
import { html, useEffect, useMemo, useState } from "../vendor/preact-htm.module.js";
import { call } from "../lib/api.js";
import { refreshProfiles, refreshProxies } from "../lib/actions.js";
import { confirmDialog, getState, toast, useStore } from "../lib/store.js";
import { relativeTime } from "../lib/util.js";
import { Icon } from "./icons.js";
import { Button, CountryChip, Empty, Field, IconButton, Menu, Select, TextArea, TextInput } from "./ui.js";

const TYPE_OPTIONS = [
  { value: "http", label: "HTTP" }, { value: "https", label: "HTTPS" },
  { value: "socks5", label: "SOCKS5" }, { value: "socks4", label: "SOCKS4" },
];

function ProxyStatus({ p }) {
  if (p.checking) return html`<span class="chip chip-busy"><span class="dot" />Checking</span>`;
  const c = p.last_check || {};
  if (!c.at) return html`<span class="chip chip-idle"><span class="dot" />Not checked</span>`;
  if (!c.ok) return html`<span class="chip chip-err" title=${c.error}><span class="dot" />Failed</span>`;
  return html`<span class="chip chip-ok"><span class="dot" />${c.latency_ms != null ? `${c.latency_ms} ms` : "Working"}</span>`;
}

function useEsc(onEsc) {
  useEffect(() => {
    const h = (e) => e.key === "Escape" && !getState().dialog && onEsc();
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  });
}

function ProxyEditor({ proxy, onClose }) {
  const isNew = !proxy;
  const [f, setF] = useState({
    name: proxy?.name || "", type: proxy?.type || "http", host: proxy?.host || "", port: proxy?.port ? String(proxy.port) : "",
    username: proxy?.username || "", password: "", change_ip_url: proxy?.change_ip_url || "", notes: proxy?.notes || "",
  });
  const [paste, setPaste] = useState("");
  const [saving, setSaving] = useState(false);
  const set = (patch) => setF((prev) => ({ ...prev, ...patch }));
  useEsc(onClose);

  const fill = async () => {
    try {
      const parts = await call("proxy_parse", paste.trim(), f.type);
      set({ type: parts.type, host: parts.host, port: String(parts.port), username: parts.username, password: parts.password });
      setPaste("");
    } catch (err) {
      toast(err.message, "error");
    }
  };
  const save = async (e, thenCheck) => {
    e && e.preventDefault();
    if (saving) return;
    setSaving(true);
    try {
      const data = { ...f, port: Number(f.port) || f.port };
      const row = isNew ? await call("proxies_create", data) : await call("proxies_update", proxy.id, data);
      toast(isNew ? `Saved "${row.name}"` : "Proxy updated", "success");
      await refreshProxies();
      await refreshProfiles();
      onClose();
      if (thenCheck) checkProxy(row.id);
    } catch (err) {
      toast(err.message, "error", 7000);
    }
    setSaving(false);
  };
  return html`<div class="overlay" onMouseDown=${(e) => e.target === e.currentTarget && onClose()}>
    <form class="drawer drawer-sm" onSubmit=${(e) => save(e, false)}>
      <header class="drawer-head">
        <div><h2>${isNew ? "Add proxy" : proxy.name}</h2><span class="muted small">${isNew ? "Saved proxies can be assigned to any profile" : `Used by ${proxy.used_by} profile${proxy.used_by === 1 ? "" : "s"}`}</span></div>
        <${IconButton} icon="x" title="Close (Esc)" onClick=${onClose} />
      </header>
      <div class="drawer-body"><div class="form">
        <${Field} label="Paste proxy" wide hint="host:port, host:port:user:pass, user:pass@host:port or scheme://user:pass@host:port">
          <div class="inline">
            <${TextInput} mono value=${paste} onInput=${setPaste} placeholder="203.0.113.10:8000:user:password" autofocus=${isNew} />
            <${Button} disabled=${!paste.trim()} onClick=${fill}>Fill<//>
          </div>
        <//>
        <${Field} label="Name" wide><${TextInput} value=${f.name} onInput=${(name) => set({ name })} placeholder="e.g. US Residential 1 (defaults to host:port)" /><//>
        <div class="grid-4">
          <${Field} label="Type"><${Select} value=${f.type} onChange=${(type) => set({ type })} options=${TYPE_OPTIONS} /><//>
          <${Field} label="Host"><${TextInput} mono value=${f.host} onInput=${(host) => set({ host })} /><//>
          <${Field} label="Port"><${TextInput} mono value=${f.port} onInput=${(port) => set({ port })} /><//>
        </div>
        <div class="grid-2">
          <${Field} label="Username"><${TextInput} value=${f.username} disabled=${f.type === "socks4"} onInput=${(username) => set({ username })} /><//>
          <${Field} label="Password" hint=${proxy?.has_password && !f.password ? "Saved — leave blank to keep it" : ""}>
            <${TextInput} type="password" value=${f.password} disabled=${f.type === "socks4"} onInput=${(password) => set({ password })} placeholder=${proxy?.has_password ? "••••••••" : ""} />
          <//>
        </div>
        <${Field} label="Change-IP URL" wide hint="Optional. For rotating proxies: the provider link that switches the exit IP.">
          <${TextInput} mono value=${f.change_ip_url} onInput=${(change_ip_url) => set({ change_ip_url })} placeholder="https://provider.example/rotate?key=…" />
        <//>
        <${Field} label="Notes" wide><${TextArea} rows=${2} value=${f.notes} onInput=${(notes) => set({ notes })} /><//>
      </div></div>
      <footer class="drawer-foot">
        <span />
        <div class="inline">
          <${Button} onClick=${onClose}>Cancel<//>
          <${Button} icon="bolt" busy=${saving} onClick=${(e) => save(e, true)}>Save & check<//>
          <${Button} kind="primary" type="submit" busy=${saving}>Save<//>
        </div>
      </footer>
    </form>
  </div>`;
}

function ImportPanel({ onClose }) {
  const [text, setText] = useState("");
  const [scheme, setScheme] = useState("http");
  const [prefix, setPrefix] = useState("");
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  useEsc(onClose);
  useEffect(() => {
    if (!text.trim()) return setPreview(null);
    const t = setTimeout(async () => {
      try { setPreview(await call("proxies_import_preview", text, scheme)); } catch (err) { setPreview({ proxies: [], errors: [{ line: 0, text: "", error: err.message }] }); }
    }, 250);
    return () => clearTimeout(t);
  }, [text, scheme]);
  const doImport = async () => {
    setBusy(true);
    try {
      const res = await call("proxies_import", text, scheme, prefix, true);
      toast(`Imported ${res.created} prox${res.created === 1 ? "y" : "ies"}${res.errors.length ? `, ${res.errors.length} line(s) skipped` : ""}. Checking them now…`, "success", 6000);
      await refreshProxies();
      onClose();
    } catch (err) {
      toast(err.message, "error");
    }
    setBusy(false);
  };
  const count = preview?.proxies.length || 0;
  return html`<div class="overlay" onMouseDown=${(e) => e.target === e.currentTarget && onClose()}>
    <div class="drawer drawer-sm">
      <header class="drawer-head">
        <div><h2>Import proxies</h2><span class="muted small">One per line. Duplicates of saved proxies are skipped.</span></div>
        <${IconButton} icon="x" title="Close (Esc)" onClick=${onClose} />
      </header>
      <div class="drawer-body"><div class="form">
        <${Field} label="Proxy list" wide hint="host:port · host:port:user:pass · user:pass@host:port · socks5://user:pass@host:port">
          <${TextArea} mono rows=${10} value=${text} onInput=${setText} placeholder=${"203.0.113.10:8000:user:pass\n203.0.113.11:8000:user:pass"} />
        <//>
        <div class="grid-2">
          <${Field} label="Type for lines without a scheme"><${Select} value=${scheme} onChange=${setScheme} options=${TYPE_OPTIONS} /><//>
          <${Field} label="Name prefix" hint="Names become prefix + host:port"><${TextInput} value=${prefix} onInput=${setPrefix} placeholder="e.g. US-" /><//>
        </div>
        ${preview ? html`<div class="import-preview">
          <div class="inline"><${Icon} name="check" class="text-ok" /><strong>${count} proxy line${count === 1 ? "" : "s"} recognised</strong></div>
          ${preview.errors.length ? html`<div class="import-errors">
            ${preview.errors.slice(0, 8).map((e) => html`<div><span class="mono">Line ${e.line}</span> <span class="muted">${e.text}</span> — <span class="text-err">${e.error}</span></div>`)}
            ${preview.errors.length > 8 ? html`<div class="muted">…and ${preview.errors.length - 8} more</div>` : null}
          </div>` : null}
        </div>` : null}
      </div></div>
      <footer class="drawer-foot">
        <span class="muted small">Imported proxies are checked automatically.</span>
        <div class="inline">
          <${Button} onClick=${onClose}>Cancel<//>
          <${Button} kind="primary" icon="upload" busy=${busy} disabled=${!count} onClick=${doImport}>Import ${count || ""}<//>
        </div>
      </footer>
    </div>
  </div>`;
}

async function checkProxy(id) {
  try {
    const row = await call("proxies_check", id);
    const r = row.result;
    toast(r.ok ? `${row.name}: ${r.ip} · ${[r.city, r.country].filter(Boolean).join(", ")} · ${r.latency_ms} ms` : `${row.name}: ${r.error}`, r.ok ? "success" : "error", 6000);
  } catch (err) {
    toast(err.message, "error");
  }
  refreshProxies();
  refreshProfiles();
}

async function rotateProxy(p) {
  toast(`Rotating ${p.name}…`, "info");
  try {
    const row = await call("proxies_rotate", p.id);
    toast(row.result.ok ? `${p.name}: new IP ${row.result.ip}` : `${p.name}: ${row.result.error}`, row.result.ok ? "success" : "error", 6000);
  } catch (err) {
    toast(err.message, "error", 7000);
  }
  refreshProxies();
}

async function deleteProxy(p) {
  const ok = await confirmDialog({
    title: "Delete proxy?",
    message: p.used_by
      ? `"${p.name}" is used by ${p.used_by} profile${p.used_by === 1 ? "" : "s"}. They will not switch to your real connection; they'll refuse to start until you choose another proxy.`
      : `"${p.name}" will be removed.`,
    confirmLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    await call("proxies_delete", p.id, true);
    toast("Proxy deleted", "success");
  } catch (err) {
    toast(err.message, "error");
  }
  refreshProxies();
  refreshProfiles();
}

export function ProxiesView() {
  const proxies = useStore((s) => s.proxies);
  const [editing, setEditing] = useState(undefined); // undefined = closed, null = new, object = edit
  const [importing, setImporting] = useState(false);
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? proxies.filter((p) => [p.name, p.host, p.type, p.last_check?.ip, p.last_check?.country, p.last_check?.city].join(" ").toLowerCase().includes(q)) : proxies;
  }, [proxies, query]);
  const checking = proxies.filter((p) => p.checking).length;
  const checkAll = async () => {
    try {
      const res = await call("proxies_check_all");
      toast(`Checking ${res.started} prox${res.started === 1 ? "y" : "ies"}…`, "info");
      refreshProxies();
    } catch (err) {
      toast(err.message, "error");
    }
  };
  return html`<section class="page">
    <header class="page-head">
      <div><h1>Proxies</h1><p class="muted">Save proxies once and assign them to any number of profiles.</p></div>
      <div class="inline">
        <${Button} icon="upload" onClick=${() => setImporting(true)}>Import<//>
        <${Button} kind="primary" icon="plus" onClick=${() => setEditing(null)}>Add proxy<//>
      </div>
    </header>
    <div class="toolbar">
      <div class="search">
        <${Icon} name="search" />
        <input class="input" placeholder="Search name, host, IP, country…" value=${query} onInput=${(e) => setQuery(e.currentTarget.value)} />
      </div>
      <${Button} icon="bolt" busy=${checking > 0} disabled=${!proxies.length} onClick=${checkAll}>${checking ? `Checking ${checking}…` : "Check all"}<//>
      <div class="toolbar-spacer" />
      <span class="muted small">${proxies.length} saved</span>
    </div>
    <div class="table-card">
      ${proxies.length === 0
        ? html`<${Empty} icon="proxy" title="No saved proxies" action=${html`<div class="inline">
            <${Button} icon="upload" onClick=${() => setImporting(true)}>Import a list<//>
            <${Button} kind="primary" icon="plus" onClick=${() => setEditing(null)}>Add proxy<//></div>`}>
            Add proxies here, then pick them in a profile's Network tab. Each profile can also use its own custom proxy.<//>`
        : filtered.length === 0
        ? html`<${Empty} icon="search" title="No matching proxies" />`
        : html`<table class="table">
            <thead><tr><th>Name</th><th>Status</th><th>Address</th><th>Exit location</th><th>Profiles</th><th class="col-actions" /></tr></thead>
            <tbody>${filtered.map((p) => html`<tr key=${p.id} onClick=${(e) => !e.target.closest("button, .menu") && setEditing(p)}>
              <td class="col-name"><div class="name">${p.name}</div><div class="sub">${p.notes || ""}</div></td>
              <td class="nowrap"><${ProxyStatus} p=${p} /><div class="sub">${p.last_check?.at ? `Checked ${relativeTime(p.last_check.at).replace(/^Just now$/, "just now")}` : ""}</div></td>
              <td class="nowrap"><span class="pill">${p.type.toUpperCase()}</span> <span class="mono">${p.host}:${p.port}</span>${p.has_password ? html` <span class="muted small" title="Uses a username and password"><${Icon} name="shield" size=${12} /></span>` : null}</td>
              <td>${p.last_check?.ok
                ? html`<div class="proxy-cell"><${CountryChip} code=${p.last_check.country_code} /><span class="mono">${p.last_check.ip}</span></div>
                    <div class="sub">${[p.last_check.city, p.last_check.timezone].filter(Boolean).join(" · ")}</div>`
                : p.last_check?.error ? html`<span class="text-err small" title=${p.last_check.error}>${p.last_check.error.slice(0, 48)}</span>` : html`<span class="muted">—</span>`}</td>
              <td>${p.used_by || html`<span class="muted">0</span>`}</td>
              <td class="col-actions">
                <${Button} size="sm" icon="bolt" busy=${p.checking} onClick=${() => checkProxy(p.id)}>Check<//>
                <${Menu} items=${[
                  { icon: "edit", label: "Edit", onClick: () => setEditing(p) },
                  p.change_ip_url ? { icon: "refresh", label: "Rotate IP", onClick: () => rotateProxy(p) } : null,
                  "-",
                  { icon: "trash", label: "Delete", danger: true, onClick: () => deleteProxy(p) },
                ]} />
              </td>
            </tr>`)}</tbody>
          </table>`}
    </div>
    ${editing !== undefined ? html`<${ProxyEditor} proxy=${editing} onClose=${() => setEditing(undefined)} />` : null}
    ${importing ? html`<${ImportPanel} onClose=${() => setImporting(false)} />` : null}
  </section>`;
}
