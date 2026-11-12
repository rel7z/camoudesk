// Minimal global store: one state object, shallow updates, subscribers re-render via hooks.
import { useLayoutEffect, useReducer } from "../vendor/preact-htm.module.js";

let state = {
  view: "profiles", // profiles | trash | proxies | settings
  appInfo: null,
  profiles: [],
  trash: [],
  proxies: [],
  settings: null,
  loaded: false,
  selection: new Set(),
  pending: {}, // profile id -> "launch" | "stop" | "delete"
  filters: { query: "", os: "all", folder: "all", tag: "all", sort: "name" }, // stable order: rows never jump on launch
  drawer: null, // { id: string|null (null = new), tab: string }
  toasts: [],
  dialog: null, // { title, message, confirmLabel, danger, resolve }
};

const listeners = new Set();

export const getState = () => state;

export function setState(patch) {
  const next = typeof patch === "function" ? patch(state) : patch;
  state = { ...state, ...next };
  listeners.forEach((l) => l());
}

export function useStore(selector = (s) => s) {
  const [, force] = useReducer((x) => x + 1, 0);
  // Layout effect: subscribes synchronously after render, so no update can slip in between
  // render and subscription (plain effects wait for a paint timer, which hidden views suspend).
  useLayoutEffect(() => {
    listeners.add(force);
    force(); // catch up with anything that changed before we subscribed
    return () => listeners.delete(force);
  }, []);
  return selector(state);
}

// ---------------------------------------------------------------- toasts & dialogs

let toastSeq = 0;
export function toast(message, kind = "info", ms = 4200) {
  const id = ++toastSeq;
  setState((s) => ({ toasts: [...s.toasts, { id, message, kind }].slice(-4) }));
  setTimeout(() => setState((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), ms);
}

export function confirmDialog({ title, message, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => setState({ dialog: { title, message, confirmLabel, danger, resolve } }));
}

// Resolves to the entered text, or null when cancelled.
export function promptDialog({ title, message = "", label, initial = "", placeholder = "", confirmLabel = "Save", allowEmpty = false }) {
  return new Promise((resolve) =>
    setState({ dialog: { title, message, confirmLabel, input: { label, value: initial, placeholder, allowEmpty }, resolve } })
  );
}

export function setPending(id, what) {
  setState((s) => {
    const pending = { ...s.pending };
    if (what) pending[id] = what;
    else delete pending[id];
    return { pending };
  });
}
