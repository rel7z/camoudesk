// Left navigation.
import { html } from "../vendor/preact-htm.module.js";
import { setState, useStore } from "../lib/store.js";
import { Icon } from "./icons.js";

const NAV = [
  { view: "profiles", label: "Profiles", icon: "profiles" },
  { view: "proxies", label: "Proxies", icon: "proxy" },
  { view: "settings", label: "Settings", icon: "settings" },
];

export function Sidebar() {
  const { view, profiles, proxies, appInfo } = useStore((s) => s);
  const running = profiles.filter((p) => p.status !== "stopped").length;
  const counts = { profiles: profiles.length, proxies: proxies.length };
  return html`<nav class="sidebar" aria-label="Main">
    <div class="brand">
      <div class="brand-mark"><${Icon} name="logo" size=${18} /></div>
      <div>
        <div class="brand-name">CamouDesk</div>
        <div class="brand-sub">${appInfo ? `v${appInfo.version}` : ""}</div>
      </div>
    </div>
    <ul class="nav">
      ${NAV.map(
        (n) => html`<li><button type="button" class=${view === n.view ? "is-active" : ""} aria-current=${view === n.view ? "page" : undefined}
          onClick=${() => setState({ view: n.view, selection: new Set() })}>
          <${Icon} name=${n.icon} /><span>${n.label}</span>
          ${counts[n.view] ? html`<span class="nav-count">${counts[n.view]}</span>` : null}
        </button></li>`
      )}
    </ul>
    <div class="sidebar-foot">
      <div class=${`running-pill ${running ? "is-on" : ""}`}><span class="dot" />${running ? `${running} running` : "Nothing running"}</div>
    </div>
  </nav>`;
}
