# Canoniq docs site

This is the [Docusaurus](https://docusaurus.io/) site that renders the
canoniq documentation. **The content lives in [`../docs/`](../docs/)**,
not here — this directory is presentation only (config, theme, static
assets). See [`../docs/README.md`](../docs/README.md) for how the two fit
together.

Published at **https://kshesha1.github.io/Canoniq/**, deployed
automatically on every push to `main` that touches `docs/` or `site/` (see
[`.github/workflows/deploy-docs.yml`](../.github/workflows/deploy-docs.yml)).

## Local development

```bash
npm install
npm start
```

Starts a local dev server with live reload at `http://localhost:3000/Canoniq/`.

## Build

```bash
npm run build
```

Generates static content into `build/` (gitignored) and runs a strict
broken-link check (`onBrokenLinks: 'throw'` in `docusaurus.config.js`) —
run this locally before pushing a docs change to catch link breakage
before CI does.

## Deployment

Deployment is handled by GitHub Actions
(`actions/upload-pages-artifact` + `actions/deploy-pages`), not the
`docusaurus deploy` command — there's no `gh-pages` branch to manage
manually. Pushing to `main` is enough; the workflow builds and publishes
automatically. `npm run deploy` (using the classic `docusaurus deploy`
CLI command against a `gh-pages` branch) is not used here.
