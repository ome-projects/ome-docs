#!/usr/bin/env bash
# Generate the API reference page from the Go types of ome-projects/ome.
#
#   hack/genref/generate.sh <ome checkout> [output directory]
#
# The checkout is OME at the commit in ome.ref; the output directory defaults
# to src/lib/content/reference/api. Everything else the page needs is in this
# directory: the genref version below, config.yaml and the templates.
#
# genref resolves OME's types through OME's go.mod, and loads its templates
# from ./markdown, so it has to run in a directory of the checkout that holds
# the templates. The script makes a scratch one and removes it afterwards.
set -euo pipefail

GENREF_VERSION=v0.28.0
PAGE=ome.v1beta1.md

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ome="$(cd "${1:?usage: generate.sh <ome checkout> [output directory]}" && pwd)"
out="${2:-$here/../../src/lib/content/reference/api}"

if ! grep -qx 'module sigs.k8s.io/ome' "$ome/go.mod" 2>/dev/null; then
  echo "error: $ome is not a checkout of ome-projects/ome" >&2
  exit 1
fi

# The templates need a recent text/template: a genref built with Go older than
# 1.24 writes no page and reports no error. Use OME's Go version unless the
# caller has chosen a toolchain, as actions/setup-go does.
export GOTOOLCHAIN="${GOTOOLCHAIN:-go$(sed -n 's/^go //p' "$ome/go.mod")}"

work="$(mktemp -d "$ome/.genref.XXXXXX")"
trap 'rm -rf "$work"' EXIT
cp -R "$here/markdown" "$work/markdown"
GOBIN="$work/bin" go install "github.com/kubernetes-sigs/reference-docs/genref@$GENREF_VERSION"
(cd "$work" && bin/genref -c "$here/config.yaml" -o "$work/out")

# genref logs a package or template error and still exits 0.
if [ ! -s "$work/out/$PAGE" ]; then
  if [ "${GITHUB_ACTIONS:-}" = true ]; then
    echo "::error title=API reference::genref wrote no page; see its log above."
  fi
  echo "error: genref wrote no page; see its log above" >&2
  exit 1
fi

mkdir -p "$out"
out="$(cd "$out" && pwd)"
cp "$work/out/$PAGE" "$out/$PAGE"
echo "Wrote $out/$PAGE"
