# Contributing to the OME documentation site

This repository holds the redesigned OME documentation site, which will be served at https://lightseek.org/ome. OME itself lives in [ome-projects/ome](https://github.com/ome-projects/ome).

Until the site launches, the live documentation is the Hugo site in `site/` of ome-projects/ome. A fix to a live page goes there. This repository holds the rewritten pages.

## Set up

You need Node 22 or newer and pnpm 10.26 or newer.

```bash
pnpm install
pnpm dev
```

The site runs at http://localhost:5173/ome.

## Write a page

Pages are Markdown files in `src/lib/content/`, and `src/lib/config/nav.ts` orders them. The Writing docs page, `src/lib/content/contributing/writing-docs.md`, covers the syntax and style.

The docs describe OME's `main` branch. Check what you write against the code in ome-projects/ome, not against the Hugo pages.

## Check your change

```bash
pnpm lint && pnpm check && pnpm test && pnpm build
```

`pnpm test` also checks every page's links, the navigation and `redirects.json`. `pnpm format` fixes formatting.

The Website workflow runs these checks on every pull request, along with two checks that need OME's Go code.

## Checks that use OME's code

- **YAML examples.** Every `yaml` block in a page is checked against OME's CRDs.
- **API reference.** `src/lib/content/reference/api/ome.v1beta1.md` is generated from the Go types in OME. Don't edit it by hand. To change it, change the doc comments in ome-projects/ome.

Both run against ome-projects/ome at the commit in `ome.ref`. Because that commit is pinned, a change in OME doesn't affect pull requests here until someone moves the pin.

To run the YAML check yourself, check out ome-projects/ome at that commit next to this repository:

```bash
(cd ../ome && make envtest &&
  KUBEBUILDER_ASSETS="$(bin/setup-envtest use "$(sed -n 's/^ENVTEST_K8S_VERSION = //p' Makefile)" -p path)" \
    go run ./hack/docs-examples -content "$OLDPWD/src/lib/content")
```

### Move the pin

Move it when a page documents something newer than the pinned commit, or when OME's API types changed.

The Move the OME Pin workflow does this once a day while OME's `main` is ahead of the pin: it opens a pull request that moves the pin to OME's newest commit and regenerates the API reference. Review and merge that pull request. If one of its checks fails, fix the pages in it.

To move the pin yourself, for example to a commit that isn't OME's newest:

1. Check out ome-projects/ome at the new commit, next to this repository.
2. Write that commit's SHA to `ome.ref`:

   ```bash
   git -C ../ome rev-parse HEAD > ome.ref
   ```

3. Regenerate the API reference:

   ```bash
   GOTOOLCHAIN="go$(sed -n 's/^go //p' ../ome/go.mod)" make -C ../ome genref
   (cd ../ome/hack/genref/website &&
     ../../../bin/genref -c ../config.yaml -o "$OLDPWD/src/lib/content/reference/api")
   ```

   `GOTOOLCHAIN` builds genref with OME's Go version. A genref built with Go older than 1.24 writes no page and reports no error.

4. Commit `ome.ref` and the page together. The workflow then checks the YAML examples against the new commit's CRDs.

## Commits and pull requests

- Sign off every commit with `git commit -s`.
- Start the pull request title with `[Docs]`, `[CI/Tests]` or `[Misc]`, and keep it to 52 characters or fewer.
- Pull requests are squash-merged.
