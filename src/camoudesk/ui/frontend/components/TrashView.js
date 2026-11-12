// Trash: profiles kept on disk until deleted forever.
import { html } from "../vendor/preact-htm.module.js";
import { purgeProfiles, restoreProfiles } from "../lib/actions.js";
import { useStore } from "../lib/store.js";
import { OS_LABELS, relativeTime } from "../lib/util.js";
import { Button, Empty } from "./ui.js";

export function TrashView() {
  const trash = useStore((s) => s.trash);
  return html`<section class="page">
    <header class="page-head">
      <div><h1>Trash</h1><p class="muted">Profiles here keep their cookies and fingerprint until you delete them forever.</p></div>
      <${Button} kind="danger-ghost" icon="trash" disabled=${!trash.length} onClick=${() => purgeProfiles([], true)}>Empty trash<//>
    </header>
    <div class="table-card">
      ${trash.length === 0
        ? html`<${Empty} icon="trash" title="Trash is empty">Deleted profiles appear here and can be restored.<//>`
        : html`<table class="table">
            <thead><tr><th>Name</th><th>System</th><th>Proxy</th><th>Deleted</th><th class="col-actions" /></tr></thead>
            <tbody>${trash.map((p) => html`<tr key=${p.id} class="no-click">
              <td class="col-name"><div class="name">${p.name}</div><div class="sub">${p.folder || p.notes || ""}</div></td>
              <td>${OS_LABELS[p.os] || p.os}</td>
              <td class=${p.proxy_mode === "none" ? "muted" : ""}>${p.proxy_label}</td>
              <td class="muted col-used">${relativeTime(p.deleted_at)}</td>
              <td class="col-actions">
                <${Button} size="sm" icon="refresh" onClick=${() => restoreProfiles([p.id])}>Restore<//>
                <${Button} size="sm" kind="danger-ghost" icon="trash" onClick=${() => purgeProfiles([p.id])}>Delete forever<//>
              </td>
            </tr>`)}</tbody>
          </table>`}
    </div>
  </section>`;
}
