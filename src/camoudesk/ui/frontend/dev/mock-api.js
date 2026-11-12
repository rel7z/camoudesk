// In-memory fake of the Python bridge for UI development (index.html?mock). Never used by the app.

// Local "YYYY-MM-DD HH:MM", matching what the Python backend writes.
const stamp = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
const now = () => stamp(new Date());
const ago = (mins) => stamp(new Date(Date.now() - mins * 60000));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const push = () => window.camoudesk && window.camoudesk.onStatusChange && window.camoudesk.onStatusChange();

const FP = {
  macos: { screen: "1710x1107", cores: 10, gpu: "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
    user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) Gecko/20100101 Firefox/158.0" },
  windows: { screen: "1920x1080", cores: 16, gpu: "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002503) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0" },
  linux: { screen: "2560x1440", cores: 8, gpu: "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)",
    user_agent: "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0" },
};

const proxies = [
  { id: "px_1", name: "US Residential 1", type: "http", host: "us1.proxy.example", port: 8000, username: "acct", password: "", has_password: true,
    change_ip_url: "", notes: "", tags: [], display: "http://acct:***@us1.proxy.example:8000",
    last_check: { ok: true, ip: "198.51.100.23", country: "United States", country_code: "US", city: "Dallas", timezone: "America/Chicago", latency_ms: 212, at: ago(30) } },
  { id: "px_2", name: "DE Mobile", type: "socks5", host: "de.mobile.example", port: 1080, username: "u", password: "", has_password: true,
    change_ip_url: "https://de.mobile.example/rotate", notes: "", tags: [], display: "socks5://u:***@de.mobile.example:1080",
    last_check: { ok: true, ip: "203.0.113.77", country: "Germany", country_code: "DE", city: "Berlin", timezone: "Europe/Berlin", latency_ms: 140, at: ago(300) } },
];

let seq = 10;
const proxyRow = (px) => ({ ...px, used_by: profiles.filter((p) => p.proxy_id === px.id).length, checking: Boolean(px.checking) });
const mk = (o) => ({
  id: `prof_${++seq}`, name: "Profile", os: "macos", folder: "", tags: [], notes: "", start_urls: ["https://pixelscan.net"],
  timezone: "auto", locale: "auto", block_webrtc: true, humanize: true, fp_overrides: {}, created_at: ago(5000), last_used_at: null,
  deleted_at: null, proxy_mode: "none", proxy_id: "", proxy_parts: null, proxy_label: "Direct", proxy_missing: false, proxy_country: "",
  effective_timezone: "", effective_locale: "", status: "stopped", pid: null, mode: null, last_error: "", warning: "",
  ...o, fingerprint_summary: FP[o.os || "macos"],
});

const withProxy = (p, px) => Object.assign(p, { proxy_mode: "saved", proxy_id: px.id, proxy_label: px.name, proxy_country: px.last_check.country_code,
  effective_timezone: px.last_check.timezone, effective_locale: px.last_check.country_code === "DE" ? "de-DE" : "en-US" });

const profiles = [
  mk({ name: "Profile 1", last_used_at: ago(3), status: "running", pid: 66601, mode: "direct" }),
  withProxy(mk({ name: "Store US 01", os: "windows", folder: "Shops", tags: ["warmed", "ads"], notes: "Main seller account", last_used_at: ago(90) }), proxies[0]),
  withProxy(mk({ name: "Store US 02", os: "windows", folder: "Shops", tags: ["new"], last_used_at: ago(60 * 26) }), proxies[0]),
  withProxy(mk({ name: "Berlin research", os: "linux", tags: ["research"], last_used_at: ago(60 * 24 * 6) }), proxies[1]),
  mk({ name: "Custom proxy test", proxy_mode: "custom", proxy_parts: { type: "socks5", host: "203.0.113.10", port: 1080, username: "me", has_password: true },
    proxy_label: "socks5://me:***@203.0.113.10:1080", last_error: "[PROXY] Upstream proxy rejected the credentials (HTTP 407)" }),
  mk({ name: "Orphaned proxy", proxy_mode: "saved", proxy_id: "px_gone", proxy_label: "Missing saved proxy", proxy_missing: true }),
];

let settings = {
  new_profile: { os: "macos", timezone: "auto", locale: "auto", block_webrtc: true, humanize: true, start_urls: ["https://pixelscan.net"] },
  theme: "system", api: { enabled: false, port: 35120, token: "" },
};

const find = (id) => {
  const p = profiles.find((x) => x.id === id);
  if (!p) throw new Error("Profile not found");
  return p;
};
const ok = (data) => ({ ok: true, data });
const fail = (error) => ({ ok: false, error });
const guard = (fn) => async (...args) => {
  await sleep(120);
  try { return ok(await fn(...args)); } catch (e) { return fail(e.message); }
};

function applyPayload(p, d) {
  Object.assign(p, {
    name: d.name ?? p.name, os: d.os ?? p.os, folder: d.folder ?? p.folder, tags: d.tags ?? p.tags, notes: d.notes ?? p.notes,
    start_urls: d.start_urls ?? p.start_urls, timezone: d.timezone ?? p.timezone, locale: d.locale ?? p.locale,
    block_webrtc: d.block_webrtc ?? p.block_webrtc, humanize: d.humanize ?? p.humanize, fingerprint_summary: FP[d.os ?? p.os],
    fp_overrides: d.fp_overrides ? Object.fromEntries(Object.entries({ ...p.fp_overrides, ...d.fp_overrides }).filter(([, v]) => v != null)) : p.fp_overrides,
  });
  if (d.proxy_mode === "none") Object.assign(p, { proxy_mode: "none", proxy_id: "", proxy_parts: null, proxy_label: "Direct", proxy_country: "", proxy_missing: false, effective_timezone: "", effective_locale: "" });
  if (d.proxy_mode === "saved") withProxy(p, proxies.find((x) => x.id === d.proxy_id) || (() => { throw new Error("Choose a saved proxy"); })());
  if (d.proxy_mode === "custom") {
    const pp = d.proxy_parts;
    if (!pp.host || !pp.port) throw new Error("Custom proxy needs a host and a port");
    Object.assign(p, { proxy_mode: "custom", proxy_id: "", proxy_parts: { ...pp, has_password: Boolean(pp.password) || p.proxy_parts?.has_password, password: undefined },
      proxy_label: `${pp.type}://${pp.username ? pp.username + ":***@" : ""}${pp.host}:${pp.port}`, proxy_missing: false });
  }
  return p;
}

export function installMockApi() {
  window.pywebview = { api: {
    app_info: guard(() => ({ version: "0.2.0-dev", data_dir: "/Users/demo/Library/Application Support/CamouDesk",
      geoip: { available: true, installed: false, path: "/Users/demo/Library/Caches/camoufox/geoip/mmdb/geolite2-combined.mmdb" } })),
    profiles_list: guard(() => profiles.map((p) => ({ ...p }))),
    profiles_get: guard((id) => ({ ...find(id) })),
    profiles_create: guard((d) => {
      if (!d.name) throw new Error("Profile name is required");
      const p = applyPayload(mk({ created_at: now() }), { ...settings.new_profile, ...d });
      profiles.unshift(p);
      return { ...p };
    }),
    profiles_update: guard((id, d) => {
      const p = find(id);
      if (p.status !== "stopped") throw new Error("Stop the running browser before editing this profile.");
      return { ...applyPayload(p, d) };
    }),
    profiles_regenerate_fingerprint: guard((id, screen) => {
      const p = find(id);
      if (screen != null) p.fp_overrides = { ...p.fp_overrides, screen: screen || undefined };
      p.fingerprint_summary = { ...p.fingerprint_summary, screen: screen || ["1440x900", "1920x1200", "2560x1600"][Math.floor(Math.random() * 3)] };
      return { ...p };
    }),
    fingerprint_options: guard((os) => ({
      gpus: { macos: [["Apple", "Apple M1, or similar"], ["ATI Technologies Inc.", "Radeon R9 200 Series, or similar"]],
        windows: [["Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar"], ["Google Inc. (Intel)", "ANGLE (Intel, Intel(R) HD Graphics 400 Direct3D11 vs_5_0 ps_5_0), or similar"]],
        linux: [["Intel", "Intel(R) HD Graphics, or similar"], ["AMD", "Radeon R9 200 Series, or similar"]] }[os],
      screens: { macos: ["1440x900", "1512x982", "1710x1107", "1920x1080"], windows: ["1366x768", "1536x864", "1920x1080", "2560x1440"], linux: ["1920x1080", "2560x1440"] }[os],
      geoip_installed: false,
    })),
    profiles_delete: guard((id) => {
      profiles.splice(profiles.indexOf(find(id)), 1);
      return { id };
    }),
    profiles_launch: guard(async (id) => {
      const p = find(id);
      if (p.proxy_missing) throw new Error("This profile's saved proxy was deleted; choose another proxy or Direct.");
      p.status = "starting"; push();
      await sleep(900);
      Object.assign(p, { status: "running", pid: 70000 + seq++, mode: "direct", last_used_at: now(), last_error: "" }); push();
      return { ok: true, pid: p.pid, mode: "direct" };
    }),
    profiles_stop: guard((id) => {
      const p = find(id);
      p.status = "stopping"; push();
      setTimeout(() => { Object.assign(p, { status: "stopped", pid: null, mode: null }); push(); }, 1600);
      return { ok: true, stopping: true };
    }),
    profiles_logs: guard((id) => `[INFO] Launching Camoufox directly profile=${id} os=${find(id).os} headless=False geoip=off\n[INFO] Network: proxy=${find(id).proxy_mode} timezone=${find(id).effective_timezone || "host"}\nJavaScript warning: resource://services-settings/Utils.sys.mjs, line 125: unreachable code after return statement`),
    profiles_diagnostics: guard((id) => `CamouDesk 0.2.0-dev\nprofile: ${id}\nproxy: ${find(id).proxy_label}`),
    proxies_list: guard(() => proxies.map(proxyRow)),
    proxies_create: guard((d) => {
      if (!d.host || !d.port) throw new Error("Proxy needs a host and a port between 1 and 65535");
      const px = { id: `px_${++seq}`, name: d.name || `${d.host}:${d.port}`, type: d.type, host: d.host, port: Number(d.port), username: d.username || "",
        password: "", has_password: Boolean(d.password), change_ip_url: d.change_ip_url || "", notes: d.notes || "", tags: [], last_check: {} };
      proxies.push(px);
      return proxyRow(px);
    }),
    proxies_update: guard((id, d) => {
      const px = proxies.find((x) => x.id === id);
      Object.assign(px, { ...d, port: Number(d.port), password: "", has_password: px.has_password || Boolean(d.password) });
      return proxyRow(px);
    }),
    proxies_delete: guard((id) => { proxies.splice(proxies.findIndex((x) => x.id === id), 1); return { deleted: true }; }),
    proxies_check: guard(async (id) => {
      const px = proxies.find((x) => x.id === id);
      px.checking = true; window.camoudesk.onProxiesChange();
      await sleep(800);
      px.checking = false;
      px.last_check = px.host.includes("dead")
        ? { ok: false, error: "Upstream proxy unreachable: ConnectionRefusedError", at: now() }
        : { ok: true, ip: `198.51.100.${(seq++ % 200) + 10}`, country: "United States", country_code: "US", city: "Ashburn", timezone: "America/New_York", latency_ms: 150 + (seq % 90), at: now() };
      return { ...proxyRow(px), result: px.last_check };
    }),
    proxies_check_all: guard(() => {
      proxies.forEach((px, i) => setTimeout(() => window.pywebview.api.proxies_check(px.id).then(() => window.camoudesk.onProxiesChange()), i * 250));
      return { started: proxies.length };
    }),
    proxies_import_preview: guard((text, scheme) => {
      const lines = text.split("\n").map((l, i) => [i + 1, l.trim()]).filter(([, l]) => l && !l.startsWith("#"));
      const ok = lines.filter(([, l]) => /:\d+/.test(l));
      return { proxies: ok.map(([, l]) => ({ display: l.replace(/:[^:@]+@/, ":***@"), type: scheme })),
        errors: lines.filter(([, l]) => !/:\d+/.test(l)).map(([i, l]) => ({ line: i, text: l, error: "Unrecognised format" })) };
    }),
    proxies_import: guard((text, scheme, prefix) => {
      const lines = text.split("\n").map((l) => l.trim()).filter((l) => /:\d+/.test(l));
      for (const l of lines) {
        const [host, port, username] = l.split(":");
        proxies.push({ id: `px_${++seq}`, name: `${prefix}${host}:${port}`, type: scheme, host, port: Number(port), username: username || "",
          password: "", has_password: Boolean(username), change_ip_url: "", notes: "", tags: [], last_check: {} });
      }
      return { created: lines.length, errors: [] };
    }),
    proxies_rotate: guard(async (id) => (await window.pywebview.api.proxies_check(id)).data),
    profiles_assign_proxy: guard((ids, proxyId) => {
      const px = proxies.find((x) => x.id === proxyId);
      ids.forEach((id) => { const p = find(id); px ? withProxy(p, px) : applyPayload(p, { proxy_mode: "none" }); });
      return { changed: ids.length, skipped_running: 0 };
    }),
    proxy_parse: guard((line, scheme) => {
      const m = line.match(/^(?:(\w+):\/\/)?(?:([^:@]+):([^@]+)@)?([^:@]+):(\d+)(?::([^:]+):(.+))?$/);
      if (!m) throw new Error("Unrecognised format; use host:port, host:port:user:pass or user:pass@host:port");
      return { type: m[1] || scheme, host: m[4], port: Number(m[5]), username: m[2] || m[6] || "", password: m[3] || m[7] || "", has_password: false };
    }),
    proxy_test: guard(async (payload) => {
      await sleep(700);
      if ((payload.proxy_parts?.host || "").includes("bad")) return { ok: false, error: "Upstream proxy unreachable: ConnectionRefusedError" };
      const px = payload.proxy_id && proxies.find((x) => x.id === payload.proxy_id);
      return px ? { ...px.last_check, ok: true, locale: px.last_check.country_code === "DE" ? "de-DE" : "en-US" } : { ok: true, ip: "203.0.113.10", country: "Indonesia", country_code: "ID", city: "Jakarta", timezone: "Asia/Jakarta", latency_ms: 184, locale: "id-ID" };
    }),
    settings_get: guard(() => settings),
    settings_update: guard((patch) => {
      settings = { ...settings, ...patch, new_profile: { ...settings.new_profile, ...(patch.new_profile || {}) }, api: { ...settings.api, ...(patch.api || {}) } };
      return settings;
    }),
    geoip_download: guard(async () => { await sleep(1200); return { available: true, installed: true, age_days: 0 }; }),
  } };
}
