# OME website

The documentation site for [OME (Open Model Engine)](https://github.com/ome-projects/ome), a Kubernetes operator for serving large language models.

**Live at [ome-project.org/docs](https://ome-project.org/docs)**

## Develop

You need Node 22 or newer and pnpm 10.26 or newer.

```bash
pnpm install
pnpm dev
```

The site runs at http://localhost:5173/docs.

## Check

```bash
pnpm lint && pnpm check && pnpm test && pnpm build
```

Pages live in `src/lib/content/`, and `src/lib/config/nav.ts` orders them.
The Writing docs page, `src/lib/content/contributing/writing-docs.md`,
covers the syntax and style.

## Deploy

Every push to `main` runs `.github/workflows/deploy.yml`: build, apply the D1
migrations to `ome-db`, and deploy to the `ome` Cloudflare Pages project. A
merged pull request is live a few minutes later. To deploy without a change,
run the Deploy workflow from the Actions tab.

https://ome-3qq.pages.dev, the project's `pages.dev` address, is the origin
behind https://ome-project.org. The root of either redirects into `/docs`
(`_redirects`).

### Where things are

The Cloudflare account that holds the `ome-project.org` zone also holds:

- the Pages project `ome`, under Workers & Pages, with `ome-project.org` as
  its custom domain;
- the D1 database `ome-db`, whose id is in `wrangler.jsonc`. Its
  `content_blocks` table holds the home page's hero copy, with fallbacks in
  `src/lib/config/home.ts`. A new migration goes in `drizzle/`
  (`pnpm db:generate`), and the next deployment applies it;
- `GITHUB_TOKEN`, a secret of the project's production environment.

The Deploy workflow signs in with `CLOUDFLARE_API_TOKEN`, which needs Pages and
D1 edit permissions on the account, and `CLOUDFLARE_ACCOUNT_ID`. Both are
secrets of this repository's `production` environment.

### The header badge

The header badge reads stars, forks and the latest release from the GitHub API
at request time, with `GITHUB_TOKEN`. Without it the calls share GitHub's
60-requests-per-hour limit per egress IP with every other Worker on
Cloudflare, and the badge hides its counts whenever GitHub can't be reached.
Locally, `pnpm preview` reads the token from `.dev.vars`.

To change the token, run
`pnpm exec wrangler pages secret put GITHUB_TOKEN --project-name ome`, paste
the new one, then run the Deploy workflow: a changed secret reaches the site
with the next deployment, not the running one. A classic token with no scopes
is enough.

### The old address

The Hugo site at https://ome-projects.github.io/ome is replaced by pages that
forward to this site. `scripts/legacy-site.ts` writes them from
`redirects.json`: one per Hugo page, plus the root, the search page and a
`404.html`. The Pages workflow of ome-projects/ome runs it on a checkout of
this repository once a day and publishes the result, so a change to
`redirects.json` reaches the old address within a day.

The layout and styles come from
[smg-docs](https://github.com/smg-project/smg-docs).
