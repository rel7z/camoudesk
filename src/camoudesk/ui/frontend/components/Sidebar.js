// Left navigation: pages, profile folders and trash.
import { html } from "../vendor/preact-htm.module.js";
import { renameFolder } from "../lib/actions.js";
import { setState, useStore } from "../lib/store.js";
import { Icon } from "./icons.js";
import { Menu } from "./ui.js";

function NavButton({ active, icon, label, count, onClick, children }) {
  return html`<li class="nav-item"><button type="button" class=${active ? "is-active" : ""} aria-current=${active ? "page" : undefined} onClick=${onClick}>
    <${Icon} name=${icon} /><span class="nav-label">${label}</span>
    ${count ? html`<span class="nav-count">${count}</span>` : null}
  </button>${children || null}</li>`;
}

export function Sidebar() {
  const { view, profiles, proxies, trash, appInfo, filters } = useStore((s) => s);
  const running = profiles.filter((p) => p.status !== "stopped" && p.status !== "busy").length;
  const folders = {};
  profiles.forEach((p) => p.folder && (folders[p.folder] = (folders[p.folder] || 0) + 1));
  const go = (patch) => setState((s) => ({ ...patch, selection: new Set(), drawer: null, filters: { ...s.filters, ...(patch.filters || {}) } }));
  const onProfiles = view === "profiles";
  return html`<nav class="sidebar" aria-label="Main">
    <div class="brand">
      <div class="brand-mark"><${Icon} name="logo" size=${18} /></div>
      <div>
        <div class="brand-name">CamouDesk</div>
        <div class="brand-sub">${appInfo ? `v${appInfo.version}` : ""}</div>
      </div>
    </div>
    <ul class="nav">
      <${NavButton} icon="profiles" label="Profiles" count=${profiles.length} active=${onProfiles && filters.folder === "all"}
        onClick=${() => go({ view: "profiles", filters: { folder: "all" } })} />
      ${Object.keys(folders).length
        ? html`<li class="nav-section">Folders</li>
          ${Object.keys(folders).sort().map((name) => html`<${NavButton} icon="folder" label=${name} count=${folders[name]}
              active=${onProfiles && filters.folder === name} onClick=${() => go({ view: "profiles", filters: { folder: name } })}>
              <div class="nav-menu"><${Menu} label=${`Folder ${name}`} items=${[{ icon: "edit", label: "Rename folder", onClick: () => renameFolder(name) }]} /></div>
            <//>`)}`
        : null}
      <li class="nav-gap" />
      <${NavButton} icon="proxy" label="Proxies" count=${proxies.length} active=${view === "proxies"} onClick=${() => go({ view: "proxies" })} />
      <${NavButton} icon="trash" label="Trash" count=${trash.length} active=${view === "trash"} onClick=${() => go({ view: "trash" })} />
      <${NavButton} icon="settings" label="Settings" active=${view === "settings"} onClick=${() => go({ view: "settings" })} />
    </ul>
    <div class="sidebar-foot">
      <div class=${`running-pill ${running ? "is-on" : ""}`}><span class="dot" />${running ? `${running} running` : "Nothing running"}</div>
    </div>
  </nav>`;
}
