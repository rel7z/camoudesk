# Vendored frontend libraries

Kept in-tree so the UI works fully offline (no CDN calls; CLAUDE.md invariant 4). Do not edit
these files; replace them wholesale when upgrading and update this table.

| File | Source | Version | License | sha256 |
|---|---|---|---|---|
| `preact-htm.module.js` | npm `htm` → `preact/standalone.module.js` (Preact + hooks + htm, single ES module) | htm 3.1.1 | Apache-2.0 (htm), MIT (Preact) | `72284e8e9079c87817145df1110f74e8a2aa040b2fc384922e18dfcb46fc1fd7` |

Fetched with `npm pack htm@3.1.1` (registry integrity
`sha512-983Vyg8NwUE7JkZ6NmOqpCZ+sh1bKv2iYTlUkzlWmA5JD2acKoxd4KVxbMmxX/85mtfdnDmTFoNKcg5DGAvxNQ==`).
