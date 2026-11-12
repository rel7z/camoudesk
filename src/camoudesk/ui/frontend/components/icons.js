// Minimal stroke icon set (24x24 grid, currentColor). Simple geometric shapes, drawn in-house.
import { html } from "../vendor/preact-htm.module.js";

const P = {
  profiles: "M4 6h16M4 12h16M4 18h10",
  proxy: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9M12 3C9.5 5.6 8.2 8.6 8.2 12s1.3 6.4 3.8 9",
  settings: "M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1",
  play: "M7 4.5v15l12-7.5z",
  stop: "M6.5 6.5h11v11h-11z",
  plus: "M12 5v14M5 12h14",
  search: "M10.5 4a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13M15.5 15.5L20 20",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  trash: "M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13M10 11v6M14 11v6",
  edit: "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",
  copy: "M8 8h11v12H8zM5 16V4h11",
  x: "M6 6l12 12M18 6L6 18",
  check: "M5 12.5l4.5 4.5L19 7.5",
  alert: "M12 3.5L2.5 20h19zM12 10v4.5M12 17.5h.01",
  info: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M12 11v6M12 7.5h.01",
  refresh: "M20 12a8 8 0 1 1-2.3-5.7M20 4v4.5h-4.5",
  terminal: "M4 5h16v14H4zM7.5 9.5l3 2.5l-3 2.5M12.5 15h4",
  folder: "M3.5 6.5h6l2 2h9v10h-17z",
  tag: "M3.5 12.5V4h8.5l8.5 8.5l-8.5 8.5zM8 8.5h.01",
  fingerprint: "M7 10.5a5 5 0 0 1 10 0v2M9.5 21c1-2 1.5-4.5 1.5-8a1 1 0 0 1 2 0c0 3.5-.5 6.5-2 9M5 14c.3-1 .5-2.2.5-3.5a6.5 6.5 0 0 1 13 0c0 4-.5 7-2 10",
  shield: "M12 3l8 3v6c0 4.5-3.5 8-8 9c-4.5-1-8-4.5-8-9V6z",
  cookie: "M12 3a9 9 0 1 0 9 9a3 3 0 0 1-3-3a3 3 0 0 1-3-3a3 3 0 0 1-3-3M8.5 10h.01M14 15.5h.01M9.5 15h.01",
  chevron: "M8 10l4 4l4-4",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  bolt: "M13 2.5L4.5 13.5H12l-1 8l8.5-11H12z",
  download: "M12 4v11M7 10.5l5 5l5-5M4.5 20h15",
  upload: "M12 20V9M7 13.5l5-5l5 5M4.5 4h15",
  logo: "M4 5l4 3l4-4l4 4l4-3v9a8 8 0 0 1-16 0zM9 12.5h.01M15 12.5h.01",
};

export function Icon({ name, size = 16, class: cls = "" }) {
  return html`<svg class=${`icon ${cls}`} width=${size} height=${size} viewBox="0 0 24 24" fill=${name === "play" ? "currentColor" : "none"}
    stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d=${P[name] || P.info} />
  </svg>`;
}
