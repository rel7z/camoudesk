// CamouDesk UI entry point.
import { html, render, useEffect } from "./vendor/preact-htm.module.js";
import { maybeInstallMock, whenReady } from "./lib/api.js";
import { openDrawer, refreshProfiles, refreshProxies, refreshSettings } from "./lib/actions.js";
import { getState, setState, useStore } from "./lib/store.js";
import { debounce } from "./lib/util.js";
import { ProfileDrawer } from "./components/ProfileDrawer.js";
import { ProfilesView, focusSearch } from "./components/ProfilesView.js";
import { ProxiesView } from "./components/ProxiesView.js";
import { SettingsView } from "./components/SettingsView.js";
import { Sidebar } from "./components/Sidebar.js";
import { DialogHost, Toasts } from "./components/ui.js";

function App() {
  const view = useStore((s) => s.view);

  useEffect(() => {
    const onKey = (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod || getState().dialog) return;
      if (e.key === "n") {
        e.preventDefault();
        setState({ view: "profiles" });
        openDrawer(null);
      } else if (e.key === "f" && !getState().drawer) {
        e.preventDefault();
        setState({ view: "profiles" });
        setTimeout(() => focusSearch(), 0);
      } else if (["1", "2", "3"].includes(e.key)) {
        e.preventDefault();
        setState({ view: ["profiles", "proxies", "settings"][Number(e.key) - 1], drawer: null });
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return html`<div class="app">
    <${Sidebar} />
    <main class="main">
      ${view === "profiles" ? html`<${ProfilesView} />` : null}
      ${view === "proxies" ? html`<${ProxiesView} />` : null}
      ${view === "settings" ? html`<${SettingsView} />` : null}
    </main>
    <${ProfileDrawer} />
    <${DialogHost} />
    <${Toasts} />
  </div>`;
}

// The backend calls this (via evaluate_js) whenever a browser starts, stops, exits or warns.
window.camoudesk = { onStatusChange: debounce(() => refreshProfiles(), 120) };

(async () => {
  await maybeInstallMock();
  render(html`<${App} />`, document.getElementById("root"));
  await whenReady();
  await Promise.all([refreshSettings(), refreshProfiles(), refreshProxies()]);
  // Pushes keep the list current; this slow poll is only a safety net.
  setInterval(refreshProfiles, 15000);
})();
