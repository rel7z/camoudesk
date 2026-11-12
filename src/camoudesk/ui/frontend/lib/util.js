// Small formatting and data helpers shared by components.

export const OS_LABELS = { macos: "macOS", windows: "Windows", linux: "Linux" };

export function relativeTime(stamp) {
  if (!stamp) return "Never";
  const t = new Date(stamp.replace(" ", "T"));
  if (isNaN(t)) return stamp;
  const s = Math.round((Date.now() - t.getTime()) / 1000);
  if (s < 60) return "Just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return t.toLocaleDateString();
}

export function gpuLabel(gpu) {
  if (!gpu) return "";
  // "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)" -> "Apple M2"
  const m = gpu.match(/Renderer:\s*([^,)]+)/) || gpu.match(/ANGLE \([^,]+,\s*([^,(]+?)(?:\s*\(|,|\))/);
  return (m ? m[1] : gpu).trim();
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

let tzCache;
export function timezones() {
  if (!tzCache) {
    try {
      tzCache = Intl.supportedValuesOf("timeZone");
    } catch {
      tzCache = ["UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
        "Europe/London", "Europe/Berlin", "Europe/Paris", "Asia/Tokyo", "Asia/Singapore", "Asia/Jakarta",
        "Australia/Sydney"];
    }
  }
  return tzCache;
}

export const LOCALES = ["en-US", "en-GB", "en-CA", "en-AU", "en-IN", "de-DE", "de-AT", "de-CH", "fr-FR",
  "fr-CA", "es-ES", "es-MX", "pt-BR", "pt-PT", "it-IT", "nl-NL", "pl-PL", "sv-SE", "da-DK", "nb-NO",
  "fi-FI", "cs-CZ", "ro-RO", "hu-HU", "el-GR", "tr-TR", "ru-RU", "uk-UA", "ar-SA", "he-IL", "hi-IN",
  "id-ID", "ms-MY", "th-TH", "vi-VN", "ja-JP", "ko-KR", "zh-CN", "zh-TW"];

export function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function sortProfiles(list, sort) {
  const by = {
    name: (a, b) => a.name.localeCompare(b.name),
    status: (a, b) => (a.status === "stopped") - (b.status === "stopped") || a.name.localeCompare(b.name),
    last_used: (a, b) => (b.last_used_at || "").localeCompare(a.last_used_at || "") || a.name.localeCompare(b.name),
    created: (a, b) => (b.created_at || "").localeCompare(a.created_at || ""),
  }[sort] || (() => 0);
  return [...list].sort(by);
}

// Stable colour per tag name (hue from a string hash), readable on both themes.
export function tagStyle(tag) {
  let h = 0;
  for (const ch of tag) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `--tag-h: ${h}`;
}
