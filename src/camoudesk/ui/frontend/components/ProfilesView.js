// Profiles page: toolbar, sortable/filterable table with selection, bulk bar.
import { html, useMemo, useRef } from "../vendor/preact-htm.module.js";
import { call } from "../lib/api.js";
import { bulk, deleteProfiles, launchProfile, openDrawer, refreshProfiles, stopProfile } from "../lib/actions.js";
import { setState, toast, useStore } from "../lib/store.js";
import { OS_LABELS, copyText, relativeTime, sortProfiles } from "../lib/util.js";
import { Icon } from "./icons.js";
import { Button, CountryChip, Empty, IconButton, Menu, Select, StatusChip } from "./ui.js";

export let focusSearch = () => {};

function effectiveStatus(p, pending) {
  if (pending === "launch" && p.status === "stopped") return "starting";
  if (pending === "stop" && p.status !== "stopped") return "stopping";
  return p.status;
}

function Toolbar({ profiles, filtered }) {
  const filters = useStore((s) => s.filters);
  const searchRef = useRef();
  focusSearch = () => searchRef.current && searchRef.current.focus();
  const set = (patch) => setState((s) => ({ filters: { ...s.filters, ...patch } }));
  const folders = [...new Set(profiles.map((p) => p.folder).filter(Boolean))].sort();
  const tags = [...new Set(profiles.flatMap((p) => p.tags || []))].sort();
  return html`<div class="toolbar">
    <div class="search">
      <${Icon} name="search" />
      <input ref=${searchRef} class="input" placeholder="Search name, notes, proxy, tag…" value=${filters.query}
        onInput=${(e) => set({ query: e.currentTarget.value })} />
      ${filters.query ? html`<button class="search-clear" aria-label="Clear search" onClick=${() => set({ query: "" })}><${Icon} name="x" size=${14} /></button>` : null}
    </div>
    <${Select} value=${filters.os} onChange=${(os) => set({ os })} options=${[
      { value: "all", label: "All systems" }, { value: "macos", label: "macOS" },
      { value: "windows", label: "Windows" }, { value: "linux", label: "Linux" }]} />
    ${folders.length ? html`<${Select} value=${filters.folder} onChange=${(folder) => set({ folder })}
      options=${[{ value: "all", label: "All folders" }, { value: "", label: "No folder" }, ...folders.map((f) => ({ value: f, label: f }))]} />` : null}
    ${tags.length ? html`<${Select} value=${filters.tag} onChange=${(tag) => set({ tag })}
      options=${[{ value: "all", label: "All tags" }, ...tags.map((t) => ({ value: t, label: t }))]} />` : null}
    <${Select} value=${filters.sort} onChange=${(sort) => set({ sort })} options=${[
      { value: "name", label: "Name" }, { value: "last_used", label: "Recently used" },
      { value: "status", label: "Running first" }, { value: "created", label: "Newest" }]} />
    <div class="toolbar-spacer" />
    <span class="muted small">${filtered.length === profiles.length ? `${profiles.length} profiles` : `${filtered.length} of ${profiles.length}`}</span>
  </div>`;
}

function Row({ p, selected, pending }) {
  const status = effectiveStatus(p, pending);
  const busy = Boolean(pending) || status === "starting" || status === "stopping";
  const live = status !== "stopped";
  const toggle = () =>
    setState((s) => {
      const selection = new Set(s.selection);
      selection.has(p.id) ? selection.delete(p.id) : selection.add(p.id);
      return { selection };
    });
  const fp = p.fingerprint_summary || {};
  const tz = p.effective_timezone || (p.timezone === "auto" ? "Host time" : p.timezone);
  return html`<tr class=${`${selected ? "is-selected" : ""} ${live ? "is-live" : ""}`} onClick=${(e) => {
      if (e.target.closest("button, input, .menu")) return;
      openDrawer(p.id);
    }}>
    <td class="col-check"><input type="checkbox" checked=${selected} onChange=${toggle} aria-label=${`Select ${p.name}`} /></td>
    <td class="col-name">
      <div class="name-line">
        <span class="name">${p.name}</span>
        ${p.folder ? html`<span class="pill pill-folder"><${Icon} name="folder" size=${12} />${p.folder}</span>` : null}
        ${(p.tags || []).slice(0, 3).map((t) => html`<span class="pill">${t}</span>`)}
      </div>
      <div class="sub">${p.notes || (p.start_urls || [])[0] || ""}</div>
    </td>
    <td class="col-status">
      <${StatusChip} status=${status} error=${p.last_error} />
      ${p.warning ? html`<span class="row-warn" title=${p.warning}><${Icon} name="alert" size=${14} /></span>` : null}
    </td>
    <td class="col-proxy">
      ${p.proxy_missing
        ? html`<span class="text-err">Missing proxy</span>`
        : html`<span class="proxy-cell"><${CountryChip} code=${p.proxy_country} /><span class=${p.proxy_mode === "none" ? "muted" : ""}>${p.proxy_label}</span></span>`}
    </td>
    <td class="col-os"><div>${OS_LABELS[p.os] || p.os}</div><div class="sub">${fp.screen || ""}</div></td>
    <td class="col-tz"><div>${tz}</div><div class="sub">${p.effective_locale || (p.locale === "auto" ? "Default locale" : p.locale)}</div></td>
    <td class="col-used muted">${relativeTime(p.last_used_at)}</td>
    <td class="col-actions">
      ${live
        ? html`<${Button} size="sm" kind="stop" icon="stop" busy=${busy} onClick=${() => stopProfile(p.id)}>${status === "stopping" ? "Stopping" : "Stop"}<//>`
        : html`<${Button} size="sm" kind="start" icon="play" busy=${busy} onClick=${() => launchProfile(p.id)}>${status === "starting" ? "Starting" : "Start"}<//>`}
      <${Menu} items=${[
        { icon: "edit", label: "Edit profile", onClick: () => openDrawer(p.id, "general") },
        { icon: "terminal", label: "Logs & diagnostics", onClick: () => openDrawer(p.id, "advanced") },
        { icon: "copy", label: "Copy diagnostics", onClick: async () => {
            try { await copyText(await call("profiles_diagnostics", p.id)); toast("Diagnostics copied", "success"); }
            catch (err) { toast(err.message, "error"); }
          } },
        "-",
        { icon: "trash", label: "Delete", danger: true, disabled: live, onClick: () => deleteProfiles([p.id]) },
      ]} />
    </td>
  </tr>`;
}

function AssignProxy({ ids }) {
  const proxies = useStore((s) => s.proxies);
  const assign = async (proxyId, label) => {
    try {
      const res = await call("profiles_assign_proxy", ids, proxyId);
      toast(`${label} assigned to ${res.changed} profile${res.changed === 1 ? "" : "s"}${res.skipped_running ? ` (${res.skipped_running} running skipped)` : ""}`, "success");
    } catch (err) {
      toast(err.message, "error");
    }
    refreshProfiles();
  };
  return html`<${Menu} up title="Assign proxy to stopped profiles"
    trigger=${(toggle) => html`<${Button} size="sm" icon="proxy" disabled=${!ids.length} onClick=${toggle}>Proxy<//>`}
    items=${[
      { icon: "shield", label: "Direct (no proxy)", onClick: () => assign("", "Direct connection") },
      proxies.length ? "-" : null,
      ...proxies.map((px) => ({ icon: "proxy", label: `${px.name}${px.last_check?.country_code ? " · " + px.last_check.country_code : ""}`, onClick: () => assign(px.id, px.name) })),
    ]} />`;
}

function BulkBar({ ids, profiles }) {
  const chosen = profiles.filter((p) => ids.includes(p.id));
  const running = chosen.filter((p) => p.status !== "stopped");
  const stopped = chosen.filter((p) => p.status === "stopped");
  return html`<div class="bulkbar" role="toolbar" aria-label="Selected profiles">
    <span><strong>${ids.length}</strong> selected</span>
    <${Button} size="sm" kind="start" icon="play" disabled=${!stopped.length} onClick=${() => bulk(stopped.map((p) => p.id), launchProfile)}>Start ${stopped.length || ""}<//>
    <${Button} size="sm" kind="stop" icon="stop" disabled=${!running.length} onClick=${() => bulk(running.map((p) => p.id), stopProfile)}>Stop ${running.length || ""}<//>
    <${AssignProxy} ids=${stopped.map((p) => p.id)} />
    <${Button} size="sm" kind="danger-ghost" icon="trash" disabled=${!stopped.length} onClick=${() => deleteProfiles(stopped.map((p) => p.id))}>Delete<//>
    <${IconButton} icon="x" title="Clear selection" onClick=${() => setState({ selection: new Set() })} />
  </div>`;
}

export function ProfilesView() {
  const { profiles, filters, selection, pending, loaded, loadError } = useStore((s) => s);
  const filtered = useMemo(() => {
    const q = filters.query.trim().toLowerCase();
    return sortProfiles(
      profiles.filter((p) => {
        if (filters.os !== "all" && p.os !== filters.os) return false;
        if (filters.folder !== "all" && (p.folder || "") !== filters.folder) return false;
        if (filters.tag !== "all" && !(p.tags || []).includes(filters.tag)) return false;
        if (!q) return true;
        return [p.name, p.notes, p.proxy_label, p.folder, ...(p.tags || []), p.effective_timezone]
          .join(" ").toLowerCase().includes(q);
      }),
      filters.sort
    );
  }, [profiles, filters]);
  const ids = [...selection];
  const allChecked = filtered.length > 0 && filtered.every((p) => selection.has(p.id));
  const toggleAll = () => setState({ selection: allChecked ? new Set() : new Set(filtered.map((p) => p.id)) });

  return html`<section class="page">
    <header class="page-head">
      <div>
        <h1>Profiles</h1>
        <p class="muted">Isolated browser identities, each with its own fingerprint, cookies and network.</p>
      </div>
      <${Button} kind="primary" icon="plus" onClick=${() => openDrawer(null)} title="New profile (⌘N)">New profile<//>
    </header>
    <${Toolbar} profiles=${profiles} filtered=${filtered} />
    <div class="table-card">
      ${!loaded
        ? html`<div class="skeleton-rows">${[0, 1, 2, 3].map(() => html`<div class="skeleton" />`)}</div>`
        : loadError && profiles.length === 0
        ? html`<${Empty} icon="alert" title="Couldn't load profiles" action=${html`<${Button} icon="refresh" onClick=${refreshProfiles}>Try again<//>`}>${loadError}<//>`
        : profiles.length === 0
        ? html`<${Empty} title="No profiles yet" action=${html`<${Button} kind="primary" icon="plus" onClick=${() => openDrawer(null)}>Create your first profile<//>`}>
            Each profile is a separate browser with its own fingerprint, cookies and proxy.<//>`
        : filtered.length === 0
        ? html`<${Empty} icon="search" title="No matching profiles">Try a different search or clear the filters.<//>`
        : html`<table class="table">
            <thead><tr>
              <th class="col-check"><input type="checkbox" checked=${allChecked} onChange=${toggleAll} aria-label="Select all" /></th>
              <th>Name</th><th>Status</th><th>Proxy</th><th>System</th><th>Time & locale</th><th>Last used</th><th class="col-actions" />
            </tr></thead>
            <tbody>${filtered.map((p) => html`<${Row} key=${p.id} p=${p} selected=${selection.has(p.id)} pending=${pending[p.id]} />`)}</tbody>
          </table>`}
    </div>
    ${ids.length ? html`<${BulkBar} ids=${ids} profiles=${profiles} />` : null}
  </section>`;
}
