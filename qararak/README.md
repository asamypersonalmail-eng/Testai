# قرارك (Qararak) — Property Decision Intelligence

A single-file, bilingual (AR/EN) real-estate decision tool: evaluate a property
to buy, or a property you already own, using location scoring, fair-value
comparables, payment-plan cash-equivalent pricing, and a "what if" scenario
simulator.

## Files

- `index.html` — the app.
- `data.json` — reference data loaded at runtime: points of interest (`pois`)
  used for location scoring, and comparable sales (`comparables`) used for
  fair-value/yield estimates. Edit this file to change the sample data —
  no changes to `index.html` are needed.

## Running locally

The app fetches `data.json` at runtime, so it must be served over HTTP —
opening `index.html` directly as a `file://` URL will fail to load the data
(browsers block `fetch()` of local files under that scheme). From this
directory:

```bash
python3 -m http.server 8080
# then open http://localhost:8080/index.html
```

Saved properties persist in the browser's `localStorage`, scoped to
whatever origin serves the page.
