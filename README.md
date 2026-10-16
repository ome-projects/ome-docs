# OME website

Documentation site for [OME — Open Model Engine](https://github.com/ome-projects/ome). (Open Model Engine) is a Kubernetes operator for enterprise-grade management and serving of Large Language Models (LLMs)

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
migrations to `ome-db`, and deploy to the `ome` Cloudflare Pages project. The
production URL is https://ome-project.org/docs; the project's `pages.dev`
address is the underlying origin. The root of either redirects into `/docs`
(`_redirects`).

The header badge reads stars, forks and the latest release from the GitHub API
at request time. The `GITHUB_TOKEN` secret of the Pages project authenticates
those calls; without it they share GitHub's 60-requests-per-hour limit per
egress IP with every other Worker on Cloudflare, and the badge hides its counts
whenever GitHub can't be reached. Locally, `pnpm preview` reads the token from
`.dev.vars`.

### The old address

The Hugo site at https://ome-projects.github.io/ome is replaced by pages that
forward to this site. `scripts/legacy-site.ts` writes them from
`redirects.json`: one per Hugo page, plus the root, the search page and a
`404.html`. The Pages workflow of ome-projects/ome runs it on a checkout of
this repository and publishes the result, so a change to `redirects.json`
reaches the old address with that workflow's next run.

### Set up once

Log in with `pnpm exec wrangler login` to the Cloudflare account that holds
the `ome-project.org` zone, then:

1. Create the database: `pnpm exec wrangler d1 create ome-db`. Put the
   `database_id` it prints into `wrangler.jsonc` in place of the placeholder,
   then run `pnpm db:migrate:remote`.
2. Create the project and deploy once:
   `pnpm exec wrangler pages project create ome --production-branch main`, then
   `pnpm run deploy`. The site is now at the project's `pages.dev` address.
3. Set the token the header badge uses:
   `pnpm exec wrangler pages secret put GITHUB_TOKEN --project-name ome`. Any
   token that can read public repositories works.
4. In this GitHub repository, create the `production` environment with the
   secrets `CLOUDFLARE_API_TOKEN` (Pages and D1 edit permissions on the
   account) and `CLOUDFLARE_ACCOUNT_ID`. From then on every push to `main`
   deploys.
5. Add `ome-project.org` as a custom domain of the Pages project, in the
   Cloudflare dashboard under Workers & Pages → ome → Custom domains. The zone
   is in the same account, so Cloudflare adds the DNS record and the
   certificate itself. Add `www.ome-project.org` the same way if it should
   work too.
6. In ome-projects/ome, replace the Hugo build in `.github/workflows/pages.yml`
   with a run of `scripts/legacy-site.ts` (see The old address), so the old
   address forwards here from its next deployment.

The layout and styles come from
[smg-docs](https://github.com/smg-project/smg-docs).
