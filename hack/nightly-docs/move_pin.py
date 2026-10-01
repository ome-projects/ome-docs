"""Open the pull request that moves the OME pin (standard library only)."""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

import nightly_docs as docs


# genref writes this page from OME's Go types. The Website workflow rejects a
# revision whose page differs from OME at the commit in ome.ref, so the pin and
# the page move together.
PAGE = docs.DOC_ROOT + "reference/api/ome.v1beta1.md"
CODE_BRANCH = "main"
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_LISTED = 50


def api(endpoint):
    return json.loads(docs.run("gh", "api", endpoint))


def newest():
    return api(f"repos/{docs.CODE_REPO}/git/ref/heads/{CODE_BRANCH}")["object"]["sha"]


def branch_for(target):
    return f"ome-pin/{target[:12]}"


def subject(commit):
    """Commit subjects are OME contributors' text: keep one printable line that
    cannot carry a comment marker into the pull request body."""
    lines = commit["commit"]["message"].splitlines() or [""]
    line = "".join(char for char in lines[0] if char.isprintable()).replace("<!--", "")
    return f'{commit["sha"][:7]} {line[:100]}'.rstrip()


def count(total):
    return f"{total} commit" + ("" if total == 1 else "s")


def decide(repo, base, target):
    """Say whether a pull request should move the pin of a documentation revision."""
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise ValueError("Expected a full OME commit SHA")
    current = docs.code_sha(base)
    decision = {"needed": False, "current": current, "target": target}
    if target == current:
        return {**decision, "reason": f"{docs.CODE_REF} already names OME commit {target[:7]}; nothing to move."}
    comparison = api(f"repos/{docs.CODE_REPO}/compare/{current}...{target}")
    if comparison["status"] != "ahead":
        # A pin off OME's main, or a rewritten main, needs a maintainer.
        raise ValueError(f"OME commit {target} is not ahead of the pinned {current}")
    # One pin move at a time, whoever wrote it. An open one is never rewritten:
    # a maintainer may have added fixes for the new commit's CRDs to it.
    prs = docs.pages(f"repos/{repo}/pulls?state=open&per_page=100")
    files = docs.open_pr_files(repo, [pr["number"] for pr in prs])
    moving = sorted(number for number, paths in files.items() if docs.CODE_REF in paths)
    if moving:
        return {**decision, "reason": f"Pull request #{moving[0]} already moves the pin."}
    owner = repo.split("/")[0]
    closed = docs.pages(f"repos/{repo}/pulls?state=closed&head={owner}:{branch_for(target)}&per_page=100")
    if closed:
        return {**decision, "reason": f'Pull request #{closed[0]["number"]} for this commit was closed; '
                                      "the next OME commit gets a new one."}
    return {**decision, "needed": True, "total": comparison["total_commits"],
            "commits": [subject(commit) for commit in comparison["commits"][:MAX_LISTED]]}


def read_page(path):
    """The page comes from the job that ran OME's generator; import it as data."""
    page = Path(path)
    if page.is_symlink() or not page.is_file():
        raise ValueError("Expected the generated API reference as a regular file")
    raw = page.read_bytes()
    if not 0 < len(raw) <= MAX_PAGE_BYTES:
        raise ValueError("The generated API reference is empty or exceeds 4 MiB")
    text = raw.decode()
    if "\x00" in text or not text.startswith("---\n"):
        raise ValueError("The generated API reference is not a Markdown page with front matter")
    return text


def description(decision):
    current, target = decision["current"], decision["target"]
    listed = "\n".join(decision["commits"])
    if decision["total"] > len(decision["commits"]):
        listed += f'\n... and {decision["total"] - len(decision["commits"])} more'
    return f'''## What this PR does

Moves `{docs.CODE_REF}` from `{current[:7]}` to `{target[:7]}` and regenerates the API reference from that commit.

## Why we need it

The nightly documentation run and the Website checks read {docs.CODE_REPO} at the commit in `{docs.CODE_REF}`. OME's `{CODE_BRANCH}` is {count(decision["total"])} ahead of it: https://github.com/{docs.CODE_REPO}/compare/{current}...{target}

```text
{listed}
```

## How to test

The Website workflow checks the YAML examples against this commit's CRDs, and that the API reference matches it. If a check fails, fix the pages in this pull request.

## Checklist

- [x] Every commit is signed off (`git commit -s`)
- [ ] `pnpm lint && pnpm check && pnpm test && pnpm build` passes (run by the Website workflow on this pull request)
'''


def summarize(text):
    print(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(text + "\n")


def publish(repo, base, base_branch, target, page):
    # Recheck immediately before publishing: a pin move may have been opened
    # or merged while the API reference was regenerated.
    decision = decide(repo, base, target)
    if not decision["needed"]:
        summarize(decision["reason"])
        return
    content = read_page(page)
    if docs.git("rev-parse", "HEAD") != base or docs.git("status", "--porcelain"):
        raise ValueError("Expected a clean checkout of the documentation baseline")
    Path(docs.CODE_REF).write_text(target + "\n")
    Path(PAGE).write_text(content)
    docs.mutate_git("add", "--", docs.CODE_REF, PAGE)
    # Never overwrite an existing branch, even after a prior push/PR API failure.
    # In that case reuse it only if its exact tree and parent match this run.
    branch = branch_for(target)
    tree = docs.git("write-tree")
    remote = docs.git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if remote:
        docs.mutate_git("fetch", "origin", f"refs/heads/{branch}")
        if docs.git("rev-parse", "FETCH_HEAD^{tree}") != tree or docs.git("rev-parse", "FETCH_HEAD^") != base:
            raise ValueError(f"Existing branch {branch} differs; inspect it before retrying")
    title = f"[Docs] Move the OME pin to {target[:7]}"
    if os.getenv("DRY_RUN") == "true":
        summarize(f"Validated dry run: {title}; no branch or PR created.")
        return
    if not remote:
        docs.mutate_git("switch", "-c", branch)
        docs.git("config", "user.name", "github-actions[bot]")
        docs.git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        docs.mutate_git("commit", "-s", "-m", title)
        docs.mutate_git("push", "origin", f"HEAD:refs/heads/{branch}")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md") as f:
        f.write(description(decision))
        f.flush()
        url = docs.run("gh", "pr", "create", "--repo", repo, "--base", base_branch,
                       "--head", branch, "--title", title, "--body-file", f.name)
    summarize(f"- {title}: {url}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "publish"])
    args = parser.parse_args()
    repo, base = os.environ["GITHUB_REPOSITORY"], os.environ["BASE_SHA"]
    if args.command == "plan":
        decision = decide(repo, base, newest())
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(f'needed={str(decision["needed"]).lower()}\n')
            out.write(f'target={decision["target"]}\n')
        summarize(decision.get("reason") or
                  f'{docs.CODE_REF} is {count(decision["total"])} behind OME; moving it to {decision["target"]}.')
    else:
        publish(repo, base, os.environ["BASE_BRANCH"], os.environ["TARGET_SHA"], os.environ["GENERATED_PAGE"])


if __name__ == "__main__":
    main()
