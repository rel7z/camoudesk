// Side drawer to create or edit a profile, with tabs.
import { html, useEffect, useMemo, useRef, useState } from "../vendor/preact-htm.module.js";
import { call } from "../lib/api.js";
import { closeDrawer, refreshProfiles, saveProfile, stopProfile } from "../lib/actions.js";
import { confirmDialog, getState, setState, toast, useStore } from "../lib/store.js";
import { LOCALES, OS_LABELS, copyText, gpuLabel, timezones } from "../lib/util.js";
import { Icon } from "./icons.js";
import { Button, CountryChip, Field, IconButton, Segmented, Select, Tabs, TextArea, TextInput, Toggle } from "./ui.js";

const TABS = [
  { value: "general", label: "General", icon: "edit" },
  { value: "network", label: "Network", icon: "proxy" },
  { value: "fingerprint", label: "Fingerprint", icon: "fingerprint" },
  { value: "advanced", label: "Advanced", icon: "terminal" },
];

function formFrom(row, defaults) {
  const src = row || defaults || {};
  const parts = row?.proxy_parts || {};
  return {
    name: row?.name || "",
    os: src.os || "macos",
    folder: row?.folder || "",
    tags: (row?.tags || []).join(", "),
    notes: row?.notes || "",
    start_urls: (src.start_urls || []).join("\n"),
    proxy_mode: row?.proxy_mode || "none",
    proxy_id: row?.proxy_id || "",
    p_type: parts.type || "http",
    p_host: parts.host || "",
    p_port: parts.port ? String(parts.port) : "",
    p_user: parts.username || "",
    p_pass: "",
    p_has_pass: Boolean(parts.has_password),
    timezone: src.timezone || "auto",
    locale: src.locale || "auto",
    block_webrtc: src.block_webrtc ?? true,
    humanize: src.humanize ?? true,
  };
}

function toPayload(f, check) {
  const payload = {
    name: f.name.trim(),
    os: f.os,
    folder: f.folder.trim(),
    tags: f.tags.split(",").map((t) => t.trim()).filter(Boolean),
    notes: f.notes,
    start_urls: f.start_urls.split("\n").map((u) => u.trim()).filter(Boolean),
    proxy_mode: f.proxy_mode,
    timezone: f.timezone.trim() || "auto",
    locale: f.locale.trim() || "auto",
    block_webrtc: f.block_webrtc,
    humanize: f.humanize,
  };
  if (f.proxy_mode === "saved") payload.proxy_id = f.proxy_id;
  if (f.proxy_mode === "custom") {
    payload.proxy_parts = { type: f.p_type, host: f.p_host.trim(), port: f.p_port.trim(), username: f.p_user, password: f.p_pass };
    if (check && check.ok) payload.proxy_check = check;
  }
  return payload;
}

function CheckResult({ result }) {
  if (!result) return null;
  if (result.pending) return html`<div class="check-card"><span class="spinner" /> Checking connection through the proxy…</div>`;
  if (!result.ok) return html`<div class="check-card is-err"><${Icon} name="alert" /><span>${result.error}</span></div>`;
  return html`<div class="check-card is-ok">
    <${Icon} name="check" />
    <div class="check-grid">
      <span>Exit IP</span><strong class="mono">${result.ip}</strong>
      <span>Location</span><strong><${CountryChip} code=${result.country_code} /> ${[result.city, result.country].filter(Boolean).join(", ") || "Unknown"}</strong>
      <span>Timezone</span><strong>${result.timezone || "Unknown"}</strong>
      <span>Latency</span><strong>${result.latency_ms} ms</strong>
    </div>
  </div>`;
}

function NetworkTab({ f, set, row, disabled, check, setCheck }) {
  const proxies = useStore((s) => s.proxies);
  const [paste, setPaste] = useState("");
  const tzList = useMemo(() => timezones(), []);
  const applyPaste = async () => {
    if (!paste.trim()) return;
    try {
      const parts = await call("proxy_parse", paste.trim(), f.p_type);
      set({ p_type: parts.type, p_host: parts.host, p_port: String(parts.port), p_user: parts.username, p_pass: parts.password, p_has_pass: false });
      setPaste("");
      setCheck(null);
    } catch (err) {
      toast(err.message, "error");
    }
  };
  const test = async () => {
    setCheck({ pending: true });
    try {
      const payload = f.proxy_mode === "saved"
        ? { proxy_id: f.proxy_id }
        : { proxy_parts: toPayload(f).proxy_parts, profile_id: row?.id };
      const res = await call("proxy_test", payload);
      setCheck(res);
      if (res.ok && f.proxy_mode === "saved") call("proxies_list").then((p) => setState({ proxies: p }));
    } catch (err) {
      setCheck({ ok: false, error: err.message });
    }
  };
  const saved = proxies.find((p) => p.id === f.proxy_id);
  const autoTz = f.timezone === "auto";
  const autoLocale = f.locale === "auto";
  const effective = (check && check.ok) ? check : (saved?.last_check?.ok ? saved.last_check : null);
  return html`<div class="form">
    <${Field} label="Proxy" wide>
      <${Segmented} value=${f.proxy_mode} disabled=${disabled} onChange=${(proxy_mode) => { set({ proxy_mode }); setCheck(null); }} options=${[
        { value: "none", label: "Direct", icon: "shield" },
        { value: "saved", label: "Saved proxy", icon: "proxy" },
        { value: "custom", label: "Custom", icon: "edit" }]} />
    <//>
    ${f.proxy_mode === "none" ? html`<p class="note"><${Icon} name="info" /> Traffic uses this Mac's own connection and IP address. "Auto" timezone and locale follow this Mac.</p>` : null}
    ${f.proxy_mode === "saved"
      ? proxies.length
        ? html`<${Field} label="Saved proxy" wide hint=${saved?.last_check?.at ? `Last checked ${saved.last_check.at}` : "Not checked yet"}>
            <${Select} value=${f.proxy_id} disabled=${disabled} onChange=${(proxy_id) => { set({ proxy_id }); setCheck(null); }}
              options=${[{ value: "", label: "Choose a proxy…" }, ...proxies.map((p) => ({
                value: p.id, label: `${p.name} — ${p.type.toUpperCase()} ${p.host}:${p.port}${p.last_check?.country_code ? " · " + p.last_check.country_code : ""}` }))]} />
          <//>`
        : html`<p class="note"><${Icon} name="info" /> No saved proxies yet. Add them on the Proxies page, or use Custom.</p>`
      : null}
    ${f.proxy_mode === "custom"
      ? html`
        <${Field} label="Paste proxy" wide hint="host:port, host:port:user:pass, user:pass@host:port or socks5://user:pass@host:port">
          <div class="inline">
            <${TextInput} mono value=${paste} disabled=${disabled} onInput=${setPaste} placeholder="203.0.113.10:8000:user:password" />
            <${Button} disabled=${disabled || !paste.trim()} onClick=${applyPaste}>Fill<//>
          </div>
        <//>
        <div class="grid-4">
          <${Field} label="Type"><${Select} value=${f.p_type} disabled=${disabled} onChange=${(p_type) => set({ p_type })} options=${[
            { value: "http", label: "HTTP" }, { value: "https", label: "HTTPS" }, { value: "socks5", label: "SOCKS5" }, { value: "socks4", label: "SOCKS4" }]} /><//>
          <${Field} label="Host"><${TextInput} mono value=${f.p_host} disabled=${disabled} onInput=${(p_host) => set({ p_host })} placeholder="proxy.example.com" /><//>
          <${Field} label="Port"><${TextInput} mono value=${f.p_port} disabled=${disabled} onInput=${(p_port) => set({ p_port })} placeholder="8000" /><//>
        </div>
        <div class="grid-2">
          <${Field} label="Username"><${TextInput} value=${f.p_user} disabled=${disabled || f.p_type === "socks4"} onInput=${(p_user) => set({ p_user })} /><//>
          <${Field} label="Password" hint=${f.p_has_pass && !f.p_pass ? "Saved — leave blank to keep it" : ""}>
            <${TextInput} type="password" value=${f.p_pass} disabled=${disabled || f.p_type === "socks4"} onInput=${(p_pass) => set({ p_pass })}
              placeholder=${f.p_has_pass ? "••••••••" : ""} /><//>
        </div>`
      : null}
    ${f.proxy_mode !== "none"
      ? html`<div class="inline">
          <${Button} icon="bolt" disabled=${disabled || (f.proxy_mode === "saved" ? !f.proxy_id : !f.p_host || !f.p_port)} busy=${check?.pending} onClick=${test}>Check proxy<//>
          <span class="muted small">Confirms the proxy works and finds its exit location.</span>
        </div>
        <${CheckResult} result=${check} />`
      : null}
    <div class="divider" />
    <div class="grid-2">
      <${Field} label="Timezone" hint=${autoTz ? (f.proxy_mode === "none" ? "Auto: this Mac's timezone" : `Auto: ${effective?.timezone || (check ? "" : row?.effective_timezone) || "matched to the proxy at launch"}`) : ""}>
        <${TextInput} value=${f.timezone} list="tz-list" disabled=${disabled} onInput=${(timezone) => set({ timezone })} />
        <datalist id="tz-list"><option value="auto">Auto (match network)</option>${tzList.map((t) => html`<option value=${t} />`)}</datalist>
      <//>
      <${Field} label="Language / locale" hint=${autoLocale ? (f.proxy_mode === "none" ? "Auto: browser default" : `Auto: ${effective?.locale || (check ? "" : row?.effective_locale) || "from the proxy's country"}`) : ""}>
        <${TextInput} value=${f.locale} list="locale-list" disabled=${disabled} onInput=${(locale) => set({ locale })} />
        <datalist id="locale-list"><option value="auto">Auto (match network)</option>${LOCALES.map((l) => html`<option value=${l} />`)}</datalist>
      <//>
    </div>
  </div>`;
}

function GeneralTab({ f, set, disabled, isNew }) {
  const profiles = useStore((s) => s.profiles);
  const folders = [...new Set(profiles.map((p) => p.folder).filter(Boolean))];
  return html`<div class="form">
    <${Field} label="Name" wide><${TextInput} autofocus=${isNew} value=${f.name} disabled=${disabled} onInput=${(name) => set({ name })} placeholder="e.g. Store US 01" /><//>
    <div class="grid-2">
      <${Field} label="Operating system" hint=${isNew ? "The fingerprint is generated for this system." : "Changing it creates a new fingerprint."}>
        <${Select} value=${f.os} disabled=${disabled} onChange=${(os) => set({ os })} options=${Object.entries(OS_LABELS).map(([value, label]) => ({ value, label }))} />
      <//>
      <${Field} label="Folder">
        <${TextInput} value=${f.folder} list="folder-list" disabled=${disabled} onInput=${(folder) => set({ folder })} placeholder="None" />
        <datalist id="folder-list">${folders.map((x) => html`<option value=${x} />`)}</datalist>
      <//>
    </div>
    <${Field} label="Tags" wide hint="Separate with commas"><${TextInput} value=${f.tags} disabled=${disabled} onInput=${(tags) => set({ tags })} placeholder="ads, warmed, us" /><//>
    <${Field} label="Start pages" wide hint="One URL per line; each opens in its own tab">
      <${TextArea} mono rows=${3} value=${f.start_urls} disabled=${disabled} onInput=${(start_urls) => set({ start_urls })} placeholder="https://example.com" />
    <//>
    <${Field} label="Notes" wide><${TextArea} rows=${3} value=${f.notes} disabled=${disabled} onInput=${(notes) => set({ notes })} /><//>
  </div>`;
}

function FingerprintTab({ row, disabled }) {
  const [busy, setBusy] = useState(false);
  if (!row) return html`<p class="note"><${Icon} name="info" /> A unique fingerprint for the chosen system is generated when you create the profile, and stays the same on every launch.</p>`;
  const fp = row.fingerprint_summary || {};
  const regenerate = async () => {
    const ok = await confirmDialog({
      title: "Generate a new fingerprint?",
      message: "Websites will see this profile as a different device. Cookies and logins are kept, but sites that track devices may ask you to verify again.",
      confirmLabel: "Generate new fingerprint",
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      await call("profiles_regenerate_fingerprint", row.id);
      await refreshProfiles();
      toast("New fingerprint generated", "success");
    } catch (err) {
      toast(err.message, "error");
    }
    setBusy(false);
  };
  return html`<div class="form">
    <div class="kv-card">
      <span>System</span><strong>${OS_LABELS[row.os] || row.os}</strong>
      <span>Screen</span><strong>${fp.screen || "—"}</strong>
      <span>CPU cores</span><strong>${fp.cores || "—"}</strong>
      <span>Graphics</span><strong title=${fp.gpu}>${gpuLabel(fp.gpu) || "—"}</strong>
      <span>User agent</span><strong class="mono wrap">${fp.user_agent || "—"}</strong>
    </div>
    <div class="inline">
      <${Button} icon="refresh" busy=${busy} disabled=${disabled} onClick=${regenerate}>Generate new fingerprint<//>
      <span class="muted small">Same fingerprint on every launch until you change it.</span>
    </div>
  </div>`;
}

function LogsPanel({ row }) {
  const [logs, setLogs] = useState("");
  const preRef = useRef();
  const load = async () => {
    try {
      setLogs(await call("profiles_logs", row.id, 400));
    } catch (err) {
      setLogs(String(err.message));
    }
  };
  useEffect(() => {
    load();
    if (row.status === "stopped") return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [row.id, row.status]);
  useEffect(() => {
    if (preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight;
  }, [logs]);
  const copyDiag = async () => {
    try {
      await copyText(await call("profiles_diagnostics", row.id));
      toast("Diagnostics copied (passwords removed)", "success");
    } catch (err) {
      toast(err.message, "error");
    }
  };
  return html`<div class="logs">
    <div class="logs-head">
      <span class="field-label">Browser log</span>
      <div class="inline">
        <${IconButton} icon="refresh" title="Refresh" onClick=${load} />
        <${IconButton} icon="copy" title="Copy log" onClick=${async () => { await copyText(logs); toast("Log copied", "success"); }} />
        <${Button} size="sm" icon="copy" onClick=${copyDiag}>Copy diagnostics<//>
      </div>
    </div>
    <pre ref=${preRef} class="log-pre">${logs || "No log yet. Start the profile to see its output."}</pre>
  </div>`;
}

function AdvancedTab({ f, set, row, disabled }) {
  const appInfo = useStore((s) => s.appInfo);
  return html`<div class="form">
    <${Toggle} checked=${f.block_webrtc} disabled=${disabled} onChange=${(block_webrtc) => set({ block_webrtc })}
      label="Block WebRTC" hint="Prevents WebRTC from revealing your real IP address." />
    <${Toggle} checked=${f.humanize} disabled=${disabled} onChange=${(humanize) => set({ humanize })}
      label="Human-like cursor movement" hint="Natural mouse paths when pages are automated." />
    ${row
      ? html`<div class="kv-card small">
          <span>Profile ID</span><strong class="mono">${row.id}</strong>
          <span>Created</span><strong>${row.created_at}</strong>
          ${appInfo ? html`<span>Data folder</span><strong class="mono wrap">${appInfo.data_dir}/profiles/${row.id}</strong>` : null}
        </div>
        <${LogsPanel} row=${row} />`
      : null}
  </div>`;
}

export function ProfileDrawer() {
  const drawer = useStore((s) => s.drawer);
  const profiles = useStore((s) => s.profiles);
  const settings = useStore((s) => s.settings);
  const row = drawer?.id ? profiles.find((p) => p.id === drawer.id) : null;
  const [f, setF] = useState(null);
  const [initial, setInitial] = useState(null);
  const [check, setCheck] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!drawer) return;
    const init = formFrom(row, settings?.new_profile);
    setF(init);
    setInitial(JSON.stringify(init));
    setCheck(null);
  }, [drawer?.id, drawer && !row]);

  const dirty = f && initial !== JSON.stringify(f);
  const requestClose = async () => {
    if (dirty && !(await confirmDialog({ title: "Discard changes?", message: "Your edits to this profile have not been saved.", confirmLabel: "Discard", danger: true }))) return;
    closeDrawer();
  };

  useEffect(() => {
    if (!drawer) return;
    const onKey = (e) => {
      if (e.key === "Escape" && !getState().dialog) requestClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  if (!drawer || !f) return null;
  const running = row && row.status !== "stopped";
  const set = (patch) => setF((prev) => ({ ...prev, ...patch }));
  const tab = drawer.tab || "general";
  const isNew = !drawer.id;

  const submit = async (e) => {
    e && e.preventDefault();
    if (running || saving) return;
    if (!f.name.trim()) {
      setState({ drawer: { ...drawer, tab: "general" } });
      toast("Give the profile a name", "error");
      return;
    }
    setSaving(true);
    try {
      const saved = await saveProfile(drawer.id, toPayload(f, check));
      toast(isNew ? `Created "${saved.name}"` : "Profile saved", "success");
      closeDrawer();
    } catch (err) {
      toast(err.message, "error", 7000);
    }
    setSaving(false);
  };

  return html`<div class="overlay" onMouseDown=${(e) => e.target === e.currentTarget && requestClose()}>
    <form class="drawer" onSubmit=${submit} aria-label=${isNew ? "New profile" : `Edit ${row?.name}`}>
      <header class="drawer-head">
        <div>
          <h2>${isNew ? "New profile" : row?.name || "Profile"}</h2>
          ${row ? html`<span class="muted small">${OS_LABELS[row.os]} · ${row.proxy_label}</span>` : html`<span class="muted small">Settings start from your defaults</span>`}
        </div>
        <${IconButton} icon="x" title="Close (Esc)" onClick=${requestClose} />
      </header>
      ${running
        ? html`<div class="banner"><${Icon} name="info" /><span>This profile is running. Stop it to make changes.</span>
            <${Button} size="sm" kind="stop" icon="stop" onClick=${() => stopProfile(row.id)}>Stop<//></div>`
        : null}
      <${Tabs} value=${tab} onChange=${(t) => setState({ drawer: { ...drawer, tab: t } })} tabs=${TABS} />
      <div class="drawer-body">
        ${tab === "general" ? html`<${GeneralTab} f=${f} set=${set} disabled=${running} isNew=${isNew} />` : null}
        ${tab === "network" ? html`<${NetworkTab} f=${f} set=${set} row=${row} disabled=${running} check=${check} setCheck=${setCheck} />` : null}
        ${tab === "fingerprint" ? html`<${FingerprintTab} row=${row} disabled=${running} />` : null}
        ${tab === "advanced" ? html`<${AdvancedTab} f=${f} set=${set} row=${row} disabled=${running} />` : null}
      </div>
      <footer class="drawer-foot">
        <span class="muted small">${dirty ? "Unsaved changes" : ""}</span>
        <div class="inline">
          <${Button} onClick=${requestClose}>Cancel<//>
          <${Button} kind="primary" type="submit" busy=${saving} disabled=${running}>${isNew ? "Create profile" : "Save"}<//>
        </div>
      </footer>
    </form>
  </div>`;
}
