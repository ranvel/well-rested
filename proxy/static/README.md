# Vendored static assets

The admin UI loads nothing from a CDN, so it works with the network down (which is
exactly when you'd look at the cache browser).

| File | Version | License | Source | sha256 of the original |
|------|---------|---------|--------|------------------------|
| `htmx.min.js` | htmx 2.0.11 | Zero-Clause BSD (0BSD) | https://unpkg.com/htmx.org@2.0.11/dist/htmx.min.js (fetched 2026-10-12) | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |

`htmx.min.js` is the upstream file with one comment line prepended naming the version
and source. To upgrade: download the new `dist/htmx.min.js`, prepend the same banner
with the new version and checksum, and update this table.

`style.css` and `admin.js` are ours, under the project's MPL-2.0 like the rest of the repo. `admin.js` holds the few behaviours HTMX doesn't cover
(scheme-only fields on the identity form, copy buttons); it reads everything from `data-*`
attributes and never carries page data in script text.

`favicon.png` is r's sleepy bear (`assets/sleepy-bear.png`), downscaled to 64×64 with
`sips -Z 64`. It is also served at `/favicon.ico`.
