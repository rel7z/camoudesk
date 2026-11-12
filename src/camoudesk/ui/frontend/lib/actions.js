// Data operations shared by views. All bridge calls go through here so busy states and
// error toasts are handled consistently.
import { call } from "./api.js";
import { confirmDialog, getState, setPending, setState, toast } from "./store.js";

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

export async function deleteProfiles(ids) {
  const profiles = getState().profiles.filter((p) => ids.includes(p.id));
  if (!profiles.length) return;
  const what = profiles.length === 1 ? `"${profiles[0].name}"` : `${profiles.length} profiles`;
  const ok = await confirmDialog({
    title: profiles.length === 1 ? "Delete profile?" : "Delete profiles?",
    message: `${what} and their cookies, storage and fingerprint will be removed from this Mac. This cannot be undone.`,
    confirmLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  let failed = 0;
  for (const p of profiles) {
    setPending(p.id, "delete");
    try {
      await call("profiles_delete", p.id);
    } catch (err) {
      failed++;
      toast(`${p.name}: ${err.message}`, "error");
    }
    setPending(p.id, null);
  }
  if (failed < profiles.length) toast(`Deleted ${profiles.length - failed} profile${profiles.length - failed === 1 ? "" : "s"}`, "success");
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
