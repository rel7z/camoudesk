// Data operations shared by views. All bridge calls go through here so busy states and
// error toasts are handled consistently.
import { call } from "./api.js";
import { confirmDialog, getState, promptDialog, setPending, setState, toast } from "./store.js";

export async function refreshProfiles() {
  try {
    const profiles = await call("profiles_list");
    setState((s) => {
      // Drop selections for profiles that no longer exist; clear pending once status catches up.
      const ids = new Set(profiles.map((p) => p.id));
      const selection = new Set([...s.selection].filter((id) => ids.has(id)));
      const pending = { ...s.pending };
      for (const p of profiles) {
        const want = pending[p.id];
        if ((want === "launch" && p.status !== "stopped") || (want === "stop" && p.status === "stopped")) delete pending[p.id];
      }
      return { profiles, selection, pending, loaded: true, loadError: "" };
    });
  } catch (err) {
    // Never leave the table on an endless skeleton: show the error with a retry.
    setState({ loaded: true, loadError: err.message });
  }
}

export async function refreshProxies() {
  try {
    setState({ proxies: await call("proxies_list") });
  } catch (err) {
    toast(err.message, "error");
  }
}

export async function refreshSettings() {
  try {
    const [settings, appInfo] = await Promise.all([call("settings_get"), call("app_info")]);
    setState({ settings, appInfo });
    applyTheme(settings.theme);
  } catch (err) {
    toast(err.message, "error");
  }
}

export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.dataset.theme = theme;
  else delete root.dataset.theme;
}

export async function launchProfile(id) {
  if (getState().pending[id]) return;
  setPending(id, "launch");
  try {
    await call("profiles_launch", id);
  } catch (err) {
    toast(err.message, "error", 7000);
    setPending(id, null);
  }
  await refreshProfiles();
}

export async function stopProfile(id) {
  if (getState().pending[id]) return;
  setPending(id, "stop");
  try {
    await call("profiles_stop", id);
  } catch (err) {
    toast(err.message, "error");
    setPending(id, null);
  }
  await refreshProfiles();
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export async function refreshTrash() {
  try {
    setState({ trash: await call("profiles_trash_list") });
  } catch (err) {
    toast(err.message, "error");
  }
}

export async function deleteProfiles(ids) {
  const profiles = getState().profiles.filter((p) => ids.includes(p.id));
  if (!profiles.length) return;
  const running = profiles.filter((p) => p.status !== "stopped").length;
  const ok = await confirmDialog({
    title: profiles.length === 1 ? `Move "${profiles[0].name}" to the trash?` : `Move ${plural(profiles.length, "profile")} to the trash?`,
    message: `You can restore ${profiles.length === 1 ? "it" : "them"} from the trash with cookies and fingerprint intact.${running ? ` ${plural(running, "running browser")} will be closed first.` : ""}`,
    confirmLabel: "Move to trash",
  });
  if (!ok) return;
  profiles.forEach((p) => setPending(p.id, "delete"));
  try {
    const res = await call("profiles_trash", profiles.map((p) => p.id));
    toast(`Moved ${plural(res.trashed, "profile")} to the trash`, "success");
  } catch (err) {
    toast(err.message, "error");
  }
  profiles.forEach((p) => setPending(p.id, null));
  setState({ selection: new Set() });
  await Promise.all([refreshProfiles(), refreshTrash()]);
}

export async function restoreProfiles(ids) {
  try {
    const res = await call("profiles_restore", ids);
    toast(`Restored ${plural(res.restored, "profile")}`, "success");
  } catch (err) {
    toast(err.message, "error");
  }
  await Promise.all([refreshProfiles(), refreshTrash()]);
}

export async function purgeProfiles(ids, everything = false) {
  const n = everything ? getState().trash.length : ids.length;
  const ok = await confirmDialog({
    title: everything ? "Empty the trash?" : n === 1 ? "Delete forever?" : `Delete ${plural(n, "profile")} forever?`,
    message: `${plural(n, "profile")} and ${n === 1 ? "its" : "their"} cookies, storage and fingerprint will be erased from this Mac. This cannot be undone.`,
    confirmLabel: "Delete forever",
    danger: true,
  });
  if (!ok) return;
  try {
    const res = everything ? await call("profiles_empty_trash") : await call("profiles_purge", ids);
    toast(`Deleted ${plural(res.purged, "profile")} forever`, "success");
  } catch (err) {
    toast(err.message, "error");
  }
  await refreshTrash();
}

export async function cloneProfile(p, copyData) {
  try {
    const row = await call("profiles_clone", p.id, copyData, "");
    toast(copyData ? `Duplicated as "${row.name}" (same device, cookies and logins)` : `Created "${row.name}" with a new fingerprint`, "success", 6000);
  } catch (err) {
    toast(err.message, "error");
  }
  await refreshProfiles();
}

export async function bulkMeta(ids, patch, message) {
  try {
    const res = await call("profiles_bulk_meta", ids, patch);
    toast(message(res.changed), "success");
  } catch (err) {
    toast(err.message, "error");
  }
  await refreshProfiles();
}

export async function moveToNewFolder(ids) {
  const name = await promptDialog({ title: "New folder", label: "Folder name", placeholder: "e.g. Shops", confirmLabel: "Move" });
  if (name) await bulkMeta(ids, { folder: name }, (n) => `Moved ${plural(n, "profile")} to "${name}"`);
}

export async function addTagPrompt(ids) {
  const tag = await promptDialog({ title: "Add tag", label: "Tag", placeholder: "e.g. warmed", confirmLabel: "Add tag" });
  if (tag) await bulkMeta(ids, { add_tags: [tag] }, (n) => `Tagged ${plural(n, "profile")} "${tag}"`);
}

export async function renameFolder(old) {
  const name = await promptDialog({ title: `Rename "${old}"`, label: "Folder name", initial: old, confirmLabel: "Rename", allowEmpty: true });
  if (name === null || name === old) return;
  try {
    await call("folders_rename", old, name);
    setState((s) => ({ filters: { ...s.filters, folder: name || "" } }));
    toast(name ? `Renamed to "${name}"` : `Removed folder "${old}"`, "success");
  } catch (err) {
    toast(err.message, "error");
  }
  await refreshProfiles();
}

export async function bulk(ids, fn) {
  for (const id of ids) await fn(id);
}

export async function saveProfile(id, payload) {
  const row = id ? await call("profiles_update", id, payload) : await call("profiles_create", payload);
  await refreshProfiles();
  return row;
}

export function openDrawer(id = null, tab = "general") {
  setState({ drawer: { id, tab } });
}

export function closeDrawer() {
  setState({ drawer: null });
}
