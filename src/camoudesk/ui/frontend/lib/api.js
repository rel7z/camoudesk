// Thin client for window.pywebview.api: waits for the bridge, unwraps {ok, data|error}.

let readyResolve;
const ready = new Promise((resolve) => (readyResolve = resolve));

// pywebview creates window.pywebview.api as an empty object early and fills in the methods later,
// so "ready" means the methods exist, not merely the object. The event can also fire before this
// module runs, hence the short poll as a fallback.
const hasApi = () => Boolean(window.pywebview && window.pywebview.api && typeof window.pywebview.api.app_info === "function");
// Any bridge methods at all: an older backend may lack app_info but still deserves an
// "updated, please restart" message rather than an endless loading state.
const hasAnyMethod = () =>
  Boolean(window.pywebview && window.pywebview.api && Object.values(window.pywebview.api).some((v) => typeof v === "function"));
if (hasApi()) readyResolve();
window.addEventListener("pywebviewready", () => hasApi() && readyResolve());
let pollTicks = 0;
const readyPoll = setInterval(() => {
  pollTicks++;
  if (hasApi() || (pollTicks > 60 && hasAnyMethod())) {
    clearInterval(readyPoll);
    readyResolve();
  }
}, 50);
ready.then(() => clearInterval(readyPoll));

export const whenReady = () => ready;

export async function call(method, ...args) {
  await ready;
  const fn = window.pywebview.api[method];
  // The page and the Python backend come from the same release; a missing method means the app
  // was updated while an older copy was still running (it happened: the "Try again" screen).
  if (typeof fn !== "function") throw new Error("CamouDesk was updated while it was open. Quit it (⌘Q) and open it again.");
  const res = await fn(...args);
  if (!res || res.ok === false) throw new Error((res && res.error) || `${method} failed`);
  return "data" in res ? res.data : res;
}

// Dev harness: open index.html?mock to run the UI against fake data in a normal browser.
export async function maybeInstallMock() {
  if (!new URLSearchParams(location.search).has("mock")) return;
  const { installMockApi } = await import("../dev/mock-api.js");
  installMockApi();
  readyResolve();
}
