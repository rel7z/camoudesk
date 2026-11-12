// Thin client for window.pywebview.api: waits for the bridge, unwraps {ok, data|error}.

let readyResolve;
const ready = new Promise((resolve) => (readyResolve = resolve));

// pywebview creates window.pywebview.api as an empty object early and fills in the methods later,
// so "ready" means the methods exist, not merely the object. The event can also fire before this
// module runs, hence the short poll as a fallback.
const hasApi = () => Boolean(window.pywebview && window.pywebview.api && typeof window.pywebview.api.app_info === "function");
if (hasApi()) readyResolve();
window.addEventListener("pywebviewready", () => hasApi() && readyResolve());
const readyPoll = setInterval(() => {
  if (hasApi()) {
    clearInterval(readyPoll);
    readyResolve();
  }
}, 50);
ready.then(() => clearInterval(readyPoll));

export const whenReady = () => ready;

export async function call(method, ...args) {
  await ready;
  const fn = window.pywebview.api[method];
  if (typeof fn !== "function") throw new Error(`Bridge method missing: ${method}`);
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
