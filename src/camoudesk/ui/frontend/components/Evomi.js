// Evomi integration: targeting and session controls shared by the proxy editor and profile drawer.
import { html, useEffect, useState } from "../vendor/preact-htm.module.js";
import { call } from "../lib/api.js";
import { Field, Segmented, Select, TextInput } from "./ui.js";

export const isEvomiHost = (host) => /(^|\.)evomi(-proxy)?\.com\.?$/i.test(String(host || "").trim());
export const evomiProduct = (host) => (/^(core-residential|rpc)\./i.test(String(host || "").trim()) ? "rpc" : "rp");

const MODE_HELP = {
  sticky: "Same IP for the session lifetime, then Evomi may assign a new one. Each profile reuses its own session every launch.",
  hard: "Keeps the IP as long as possible; changes only if that IP goes offline.",
  locked: "Never changes the IP. If it goes offline, requests fail instead of switching.",
  rotating: "A new IP on every request. Avoid for logins: sites see the profile jump between IPs.",
};
const MODES = [
  { value: "sticky", label: "Sticky" }, { value: "hard", label: "Hard" },
  { value: "locked", label: "Locked" }, { value: "rotating", label: "Rotating" },
];

let regionNames = null;
export function countryName(code) {
  try {
    regionNames = regionNames || new Intl.DisplayNames(["en"], { type: "region" });
    return regionNames.of(code) || code;
  } catch (_) {
    return code;
  }
}

const targetCache = new Map();
export function useEvomiTargets(product) {
  const [targets, setTargets] = useState(targetCache.get(product) || null);
  useEffect(() => {
    let alive = true;
    if (targetCache.has(product)) {
      setTargets(targetCache.get(product));
      return;
    }
    call("providers_targets", "evomi", product)
      .then((t) => { targetCache.set(product, t); alive && setTargets(t); })
      .catch(() => alive && setTargets({ countries: [], regions: {}, cities: {}, source: "builtin" }));
    return () => { alive = false; };
  }, [product]);
  return targets;
}
export const forgetEvomiTargets = () => targetCache.clear();

function PlacePicker({ label, value, options, onChange, disabled, inheritLabel }) {
  if (options && options.length) {
    return html`<${Field} label=${label}>
      <${Select} value=${value || ""} disabled=${disabled} onChange=${onChange}
        options=${[{ value: "", label: inheritLabel }, ...options.map((o) => ({ value: o, label: o.replace(/\./g, " ") }))]} />
    <//>`;
  }
  return html`<${Field} label=${label}>
    <${TextInput} value=${(value || "").replace(/\./g, " ")} disabled=${disabled} placeholder=${inheritLabel}
      onInput=${(v) => onChange(v.trim().toLowerCase().replace(/\s+/g, "."))} />
  <//>`;
}

// value: the provider options being edited. base: for a profile, the saved proxy's defaults (the
// fields then act as overrides, "" meaning "same as the proxy").
export function EvomiFields({ value, onChange, product, base = null, disabled }) {
  const targets = useEvomiTargets(product);
  const v = value || {};
  const set = (patch) => onChange({ ...v, ...patch });
  const inherit = Boolean(base);
  const country = v.country || (inherit ? base.country || "" : "");
  const countries = targets?.countries || [];
  const anyLabel = inherit
    ? `Same as proxy (${base.country ? countryName(base.country) : "any country"})`
    : "Any country";
  const mode = v.mode || (inherit ? base.mode : "sticky") || "sticky";
  const lifetime = v.lifetime ?? (inherit ? base.lifetime : 1440);
  const placeInherit = (key) => (inherit && !v.country && base[key] ? `Same as proxy (${base[key].replace(/\./g, " ")})` : "Any");
  return html`<div class="form evomi-fields">
    <div class="grid-3">
      <${Field} label="Country">
        <${Select} value=${v.country || ""} disabled=${disabled}
          onChange=${(c) => set({ country: c, region: "", city: "" })}
          options=${[{ value: "", label: anyLabel }, ...countries.map((c) => ({ value: c, label: `${countryName(c)} (${c})` }))]} />
      <//>
      <${PlacePicker} label="Region" value=${v.region} disabled=${disabled || !country}
        options=${country ? targets?.regions?.[country] : null} inheritLabel=${placeInherit("region")}
        onChange=${(region) => set({ region })} />
      <${PlacePicker} label="City" value=${v.city} disabled=${disabled || !country}
        options=${country ? targets?.cities?.[country] : null} inheritLabel=${placeInherit("city")}
        onChange=${(city) => set({ city })} />
    </div>
    <${Field} label="Session" wide hint=${MODE_HELP[mode]}>
      <${Segmented} value=${inherit && !v.mode ? "" : mode} disabled=${disabled}
        options=${inherit ? [{ value: "", label: "As proxy" }, ...MODES] : MODES}
        onChange=${(m) => set({ mode: m })} />
    <//>
    ${mode === "sticky"
      ? html`<${Field} label="Session lifetime" hint=${inherit && !v.lifetime ? `As proxy: ${base.lifetime || 1440} minutes` : "1–1440 minutes (24 hours). After it ends Evomi may pick a new IP."}>
          <label class="lifetime"><${TextInput} value=${String(v.lifetime ?? (inherit ? "" : lifetime) ?? "")} disabled=${disabled}
            placeholder=${inherit ? String(base.lifetime || 1440) : "1440"}
            onInput=${(t) => set({ lifetime: t === "" ? "" : Number(t.replace(/\D/g, "")) || "" })} />
          <span class="muted small">minutes</span></label>
        <//>`
      : null}
    ${targets?.source === "builtin"
      ? html`<p class="note small">${targets.warning ? `${targets.warning}. ` : ""}Add an Evomi API key in Settings for the official region and city lists; otherwise type them (e.g. "new york").</p>`
      : null}
  </div>`;
}
