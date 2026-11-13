// Settings page: defaults for new profiles, GeoIP database, appearance, about.
import { html, useEffect, useMemo, useState } from "../vendor/preact-htm.module.js";
import { call } from "../lib/api.js";
import { applyTheme, refreshSettings } from "../lib/actions.js";
import { confirmDialog, setState, toast, useStore } from "../lib/store.js";
import { LOCALES, OS_LABELS, copyText, timezones } from "../lib/util.js";
import { Icon } from "./icons.js";
import { forgetEvomiTargets } from "./Evomi.js";
import { Button, Field, Segmented, Select, TextArea, TextInput, Toggle } from "./ui.js";

const SEARCH_ENGINES = [
  { value: "duckduckgo", label: "DuckDuckGo" },
  { value: "google", label: "Google" },
  { value: "bing", label: "Bing" },
  { value: "brave", label: "Brave Search" },
  { value: "startpage", label: "Startpage" },
  { value: "none", label: "None (Camoufox default: searches fail)" },
];

function Section({ title, desc, children }) {
  return html`<section class="settings-section">
    <div class="settings-intro"><h2>${title}</h2>${desc ? html`<p class="muted">${desc}</p>` : null}</div>
    <div class="settings-body">${children}</div>
  </section>`;
}

function IntegrationsSection() {
  const [st, setSt] = useState(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () => {
    try { setSt((await call("providers_status")).evomi); } catch (err) { toast(err.message, "error"); }
  };
  useEffect(() => { load(); }, []);
  if (!st) return null;
  const save = async () => {
    setBusy(true);
    try {
      const res = await call("providers_set_key", "evomi", key.trim());
      setKey("");
      forgetEvomiTargets();
      toast(`Evomi connected: ${res.countries} countries available`, "success");
      await load();
    } catch (err) {
      toast(err.message, "error", 7000);
    }
    setBusy(false);
  };
  const remove = async () => {
    const ok = await confirmDialog({ title: "Remove the Evomi API key?", message: "Proxies keep working. Region and city lists fall back to typing them in.", confirmLabel: "Remove", danger: true });
    if (!ok) return;
    await call("providers_clear_key", "evomi");
    forgetEvomiTargets();
    await load();
  };
  return html`<${Section} title="Integrations" desc="Evomi residential proxies: paste your Evomi proxy on the Proxies page and every profile using it gets its own session, so its own IP. The API key is optional; it only loads Evomi's official country, region and city lists.">
    <div class="kv-card">
      <span>Evomi API key</span>
      <strong>${st.has_key ? html`<span class="text-ok">Connected</span>${st.saved_at ? ` · saved ${st.saved_at}` : ""}` : "Not set"}</strong>
    </div>
    <${Field} label=${st.has_key ? "Replace API key" : "API key"} wide hint="From the Evomi dashboard (Settings → API). Stored on this Mac only and never shown again.">
      <div class="inline">
        <${TextInput} type="password" value=${key} onInput=${setKey} placeholder=${st.has_key ? "••••••••" : "Paste your Evomi API key"} />
        <${Button} kind="primary" busy=${busy} disabled=${!key.trim()} onClick=${save}>Verify & save<//>
        ${st.has_key ? html`<${Button} onClick=${remove}>Remove<//>` : null}
      </div>
    <//>
  <//>`;
}

function ApiSection() {
  const [st, setSt] = useState(null);
  const [port, setPort] = useState("");
  const [busy, setBusy] = useState(false);
  const load = async () => {
    try { const s = await call("api_status"); setSt(s); setPort(String(s.port)); } catch (err) { toast(err.message, "error"); }
  };
  useEffect(() => { load(); }, []);
  if (!st) return null;
  const apply = async (patch) => {
    setBusy(true);
    try {
      const next = await call("settings_update", { api: patch });
      setState({ settings: next });
      await load();
    } catch (err) {
      toast(err.message, "error");
    }
    setBusy(false);
  };
  const rotate = async () => {
    const ok = await confirmDialog({ title: "Generate a new token?", message: "Scripts using the old token will stop working until you update them.", confirmLabel: "New token", danger: true });
    if (!ok) return;
    await call("api_rotate_token");
    await load();
    toast("New API token generated", "success");
  };
  const base = `http://127.0.0.1:${st.port}`;
  const curl = `curl -H "Authorization: Bearer ${st.token || "<token>"}" ${base}/v1/profiles`;
  const sdk = `from camoudesk.sdk import open_profile\n\nwith open_profile("PROFILE_ID") as context:\n    page = context.new_page()\n    page.goto("https://example.com")`;
  return html`<${Section} title="Automation" desc="Control profiles from scripts. The REST API only listens on this Mac (127.0.0.1) and needs the token.">
    <${Toggle} checked=${st.enabled} disabled=${busy} onChange=${(enabled) => apply({ enabled })}
      label="Local REST API" hint=${st.enabled ? (st.running ? `Listening on ${base}` : st.error || "Not running") : "Off"} />
    ${st.enabled ? html`
      <div class="grid-2">
        <${Field} label="Port"><div class="inline"><${TextInput} mono value=${port} onInput=${setPort} />
          <${Button} disabled=${busy || String(st.port) === port} onClick=${() => apply({ port: Number(port) })}>Apply<//></div><//>
        <div />
      </div>
      <${Field} label="Token" wide hint="Send as: Authorization: Bearer <token>">
        <div class="inline"><${TextInput} mono value=${st.token} disabled />
          <${Button} icon="copy" onClick=${async () => { await copyText(st.token); toast("Token copied", "success"); }}>Copy<//>
          <${Button} icon="refresh" onClick=${rotate}>New token<//></div>
      <//>
      <${Field} label="Try it" wide><pre class="code">${curl}</pre><//>` : null}
    <${Field} label="Python SDK (full browser automation)" wide hint="Runs in your own script with the profile's identity, proxy and cookies. The profile shows as running here.">
      <pre class="code">${sdk.replaceAll("\\n", "\n")}</pre>
    <//>
  <//>`;
}

export function SettingsView() {
  const settings = useStore((s) => s.settings);
  const appInfo = useStore((s) => s.appInfo);
  const [np, setNp] = useState(null);
  const [saving, setSaving] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const tzList = useMemo(() => timezones(), []);

  useEffect(() => {
    if (settings) setNp({ ...settings.new_profile, start_urls: settings.new_profile.start_urls.join("\n") });
  }, [settings]);
  if (!settings || !np) return html`<section class="page"><div class="skeleton" /></section>`;

  const save = async (patch, quiet) => {
    setSaving(true);
    try {
      const next = await call("settings_update", patch);
      setState({ settings: next });
      applyTheme(next.theme);
      if (!quiet) toast("Settings saved", "success");
    } catch (err) {
      toast(err.message, "error");
    }
    setSaving(false);
  };
  const saveDefaults = () => save({ new_profile: { ...np, start_urls: np.start_urls.split("\n").map((u) => u.trim()).filter(Boolean) } });
  const geoip = appInfo?.geoip || {};
  const downloadGeoip = async () => {
    setDownloading(true);
    try {
      await call("geoip_download");
      await refreshSettings();
      toast("GeoIP database installed", "success");
    } catch (err) {
      toast(err.message, "error", 8000);
    }
    setDownloading(false);
  };

  return html`<section class="page page-narrow">
    <header class="page-head"><div><h1>Settings</h1><p class="muted">Preferences are stored on this Mac only.</p></div></header>

    <${Section} title="New profile defaults" desc="Used as the starting point whenever you create a profile.">
      <div class="grid-2">
        <${Field} label="Operating system"><${Select} value=${np.os} onChange=${(os) => setNp({ ...np, os })}
          options=${Object.entries(OS_LABELS).map(([value, label]) => ({ value, label }))} /><//>
        <div />
        <${Field} label="Timezone" hint="Auto matches the proxy, or this Mac when there is no proxy">
          <${TextInput} value=${np.timezone} list="settings-tz" onInput=${(timezone) => setNp({ ...np, timezone })} />
          <datalist id="settings-tz"><option value="auto" />${tzList.map((t) => html`<option value=${t} />`)}</datalist>
        <//>
        <${Field} label="Language / locale">
          <${TextInput} value=${np.locale} list="settings-locale" onInput=${(locale) => setNp({ ...np, locale })} />
          <datalist id="settings-locale"><option value="auto" />${LOCALES.map((l) => html`<option value=${l} />`)}</datalist>
        <//>
      </div>
      <${Field} label="Start pages" wide hint="One URL per line"><${TextArea} mono rows=${2} value=${np.start_urls} onInput=${(start_urls) => setNp({ ...np, start_urls })} /><//>
      <${Toggle} checked=${np.block_webrtc} onChange=${(block_webrtc) => setNp({ ...np, block_webrtc })} label="Block WebRTC" hint="Recommended: stops WebRTC revealing your real IP." />
      <${Toggle} checked=${np.humanize} onChange=${(humanize) => setNp({ ...np, humanize })} label="Human-like cursor movement" />
      <div><${Button} kind="primary" busy=${saving} onClick=${saveDefaults}>Save defaults<//></div>
    <//>

    <${Section} title="Location matching" desc="With the GeoIP database, proxy profiles also get matching geolocation coordinates, and WebRTC (when allowed) reports the proxy IP.">
      <div class="kv-card">
        <span>GeoIP database</span>
        <strong>${!geoip.available ? "Unavailable" : geoip.installed ? html`<span class="text-ok">Installed</span>${geoip.age_days != null ? ` · ${geoip.age_days} days old` : ""}` : "Not installed"}</strong>
        ${geoip.path ? html`<span>Location</span><strong class="mono wrap">${geoip.path}</strong>` : null}
      </div>
      <div><${Button} icon="download" busy=${downloading} disabled=${!geoip.available} onClick=${downloadGeoip}>${geoip.installed ? "Update database" : "Download database"}<//></div>
    <//>

    <${Section} title="Search" desc="Camoufox ships with web search turned off, so text typed in the address bar goes nowhere. Profiles use this engine instead. It applies the next time each profile opens and is not visible to websites.">
      <${Field} label="Address bar search engine">
        <${Select} value=${settings.search_engine} onChange=${(search_engine) => save({ search_engine })} options=${SEARCH_ENGINES} />
      <//>
    <//>

    <${IntegrationsSection} />
    <${ApiSection} />

    <${Section} title="Appearance">
      <${Segmented} value=${settings.theme} onChange=${(theme) => save({ theme }, true)} options=${[
        { value: "system", label: "System" }, { value: "dark", label: "Dark" }, { value: "light", label: "Light" }]} />
    <//>

    <${Section} title="About">
      <div class="kv-card">
        <span>Version</span><strong>${appInfo?.version || "—"}</strong>
        <span>Data folder</span><strong class="mono wrap">${appInfo?.data_dir || "—"}</strong>
      </div>
      <div><${Button} icon="copy" onClick=${async () => { await copyText(appInfo?.data_dir || ""); toast("Path copied", "success"); }}>Copy data folder path<//></div>
    <//>
  </section>`;
}
