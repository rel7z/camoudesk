// Settings page: defaults for new profiles, GeoIP database, appearance, about.
import { html, useEffect, useMemo, useState } from "../vendor/preact-htm.module.js";
import { call } from "../lib/api.js";
import { applyTheme, refreshSettings } from "../lib/actions.js";
import { setState, toast, useStore } from "../lib/store.js";
import { LOCALES, OS_LABELS, copyText, timezones } from "../lib/util.js";
import { Icon } from "./icons.js";
import { Button, Field, Segmented, Select, TextArea, TextInput, Toggle } from "./ui.js";

function Section({ title, desc, children }) {
  return html`<section class="settings-section">
    <div class="settings-intro"><h2>${title}</h2>${desc ? html`<p class="muted">${desc}</p>` : null}</div>
    <div class="settings-body">${children}</div>
  </section>`;
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
