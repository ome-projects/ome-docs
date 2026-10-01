# AGENTS.md

Guidance for coding agents (Claude Code, Codex, and others) working in this repository.

## What this is

The OME documentation site: a SvelteKit app on Cloudflare Pages, served at `lightseek.org/ome` from launch. OME (Open Model Engine) is a Kubernetes operator for serving large language models, and it lives in [ome-projects/ome](https://github.com/ome-projects/ome).

Until launch, the live documentation is the Hugo site in `site/` of ome-projects/ome. A fix to a live page goes there, not here.

## Common commands

Use Node 22 or newer and pnpm 10.26 or newer.

- `pnpm install`, then `pnpm dev` — the site at http://localhost:5173/ome.
- `pnpm lint` — prettier and eslint. `pnpm format` fixes formatting.
- `pnpm test` — vitest. It also renders every page and checks links, the navigation, `redirects.json` and the search index.
- `pnpm check` — `wrangler types` and svelte-check.
- `pnpm build` — the production build.
- Single test: `pnpm exec vitest run src/lib/docs/checks.test.ts -t "<name>"`.

Run `pnpm lint && pnpm check && pnpm test && pnpm build` before submitting.

## Layout

- `src/lib/content/` — the pages: Markdown with front matter (`title`, `navLabel`, `description`, `status`, `since`, `generated`). Follow `src/lib/content/contributing/writing-docs.md` for style and syntax.
- `src/lib/config/nav.ts` — the sidebar order. Every page is listed once.
- `src/lib/config/site.ts` — repository identity. `repo` is the product repository (GitHub badge, latest release, issues). `docsRepo` is this one (Edit and source links).
- `src/lib/markdown/` — the build-time Markdown renderer, a Vite plugin.
- `src/lib/docs/` — the page registry, the content checks and the search index.
- `src/lib/server/` — worker code: GitHub repository stats and the D1 `content_blocks` table.
- `redirects.json` — maps each Hugo page in ome-projects/ome to the page that replaces it.
- `ADOPTERS.md` — the adopters list of record. The home page's list in `src/lib/components/HomeWhy.svelte` mirrors it.

## Checks that use OME's code

`.github/workflows/website.yml` checks out ome-projects/ome at the commit in `ome.ref` and runs its tools against this repository:

- **YAML examples** — every `yaml` block in a page is checked against OME's CRDs. Fix a failing example; `check=skip` is only for the cases the Writing docs page lists.
- **API reference** — `src/lib/content/reference/api/ome.v1beta1.md` is generated from the Go types in `pkg/apis/ome/v1beta1` of ome-projects/ome. Never edit it by hand. To change it, change the doc comments there, then move `ome.ref` and regenerate the page. CONTRIBUTING.md has the commands.

The docs describe OME's `main` branch. Check every field, default, flag and output against the code in ome-projects/ome, not against the Hugo pages.

## Conventions

### Commits

- Always DCO sign off (`git commit -s`); every commit carries a `Signed-off-by:` trailer.
- Author and sign-off identity must be the GitHub noreply address from `git config user.name` / `user.email` (`<id>+<user>@users.noreply.github.com`) — never a real email address.
- Never include AI attribution: no `Co-Authored-By: Claude` (or any `noreply@anthropic.com`) trailers, no "Generated with ..." lines, in commit messages or PR bodies.

### Pull requests

- PR titles must carry one prefix: `[Docs]`, `[CI/Tests]` or `[Misc]`. Keep commit titles ≤52 characters.
- Pull requests are squash-merged.
