// Proxies page (full manager arrives in the next phase; this lists saved proxies).
import { html } from "../vendor/preact-htm.module.js";
import { useStore } from "../lib/store.js";
import { CountryChip, Empty } from "./ui.js";

export function ProxiesView() {
  const proxies = useStore((s) => s.proxies);
  return html`<section class="page">
    <header class="page-head"><div><h1>Proxies</h1><p class="muted">Saved proxies you can reuse across profiles.</p></div></header>
    <div class="table-card">
      ${proxies.length === 0
        ? html`<${Empty} icon="proxy" title="No saved proxies">Proxies you save here can be assigned to any profile.<//>`
        : html`<table class="table"><thead><tr><th>Name</th><th>Type</th><th>Host</th><th>Exit</th></tr></thead>
            <tbody>${proxies.map((p) => html`<tr key=${p.id}><td>${p.name}</td><td>${p.type.toUpperCase()}</td>
              <td class="mono">${p.host}:${p.port}</td><td><${CountryChip} code=${p.last_check?.country_code} /> ${p.last_check?.ip || "—"}</td></tr>`)}</tbody></table>`}
    </div>
  </section>`;
}
