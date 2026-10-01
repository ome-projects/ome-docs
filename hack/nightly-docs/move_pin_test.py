"""Exercise the pin move's decisions and real Git publication without remote writes."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import move_pin
from move_pin import docs


OLD, NEW = "a" * 40, "b" * 40
OLD_PAGE = "---\ntitle: OME API\ngenerated: true\n---\n\nOld types.\n"
NEW_PAGE = OLD_PAGE.replace("Old", "New")
TITLE = "[Docs] Move the OME pin to bbbbbbb"


def comparison(status="ahead", messages=("Add a field (#1)",)):
    commits = [{"sha": f"{index:07x}".ljust(40, "0"), "commit": {"message": message}}
               for index, message in enumerate(messages, 1)]
    return {"status": status, "total_commits": len(commits), "commits": commits}


class Repository(unittest.TestCase):
    """A documentation revision that pins OLD, and a GitHub that knows NEW."""

    def setUp(self):
        self.original = os.getcwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        self.addCleanup(self.cleanup)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@users.noreply.github.com")
        Path(move_pin.PAGE).parent.mkdir(parents=True)
        Path(move_pin.PAGE).write_text(OLD_PAGE)
        Path(docs.CODE_REF).write_text(OLD + "\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD")
        self.comparison = comparison()
        self.open, self.files, self.closed = [], {}, []
        for owner, name, fake in [(move_pin, "api", self.api), (docs, "pages", self.pages),
                                  (docs, "open_pr_files", lambda repo, numbers: self.files)]:
            patcher = patch.object(owner, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def cleanup(self):
        os.chdir(self.original)
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()

    def api(self, endpoint):
        if endpoint == f"repos/{docs.CODE_REPO}/git/ref/heads/{move_pin.CODE_BRANCH}":
            return {"object": {"sha": NEW}}
        self.assertEqual(endpoint, f"repos/{docs.CODE_REPO}/compare/{OLD}...{NEW}")
        return self.comparison

    def pages(self, endpoint):
        if "state=closed" in endpoint:
            self.assertIn(f"head=test:{move_pin.branch_for(NEW)}&", endpoint)
            return self.closed
        return self.open

    def decide(self, target=NEW):
        return move_pin.decide("test/repo", self.base, target)


class DecisionTests(Repository):
    def test_a_pin_at_omes_newest_commit_needs_no_pull_request(self):
        with patch.object(move_pin, "api") as remote:
            decision = self.decide(OLD)
            remote.assert_not_called()
        self.assertFalse(decision["needed"])
        self.assertIn("already names", decision["reason"])

    def test_newer_commits_need_one_pull_request(self):
        self.comparison = comparison(messages=["Add a field (#1)", "Fix it (#2)"])
        decision = self.decide(move_pin.newest())
        self.assertTrue(decision["needed"])
        self.assertEqual((decision["current"], decision["target"], decision["total"]), (OLD, NEW, 2))
        self.assertEqual(decision["commits"], ["0000001 Add a field (#1)", "0000002 Fix it (#2)"])

    def test_the_pin_only_moves_forward_along_omes_history(self):
        for status in ["behind", "diverged", "identical"]:
            self.comparison = comparison(status)
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "not ahead"):
                self.decide()
        for target in ["main", "abc1234", "", NEW + "\n" + NEW]:
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "full OME commit SHA"):
                self.decide(target)

    def test_an_open_pin_move_is_left_alone_whoever_wrote_it(self):
        self.open = [{"number": 4}, {"number": 9}]
        self.files = {4: [docs.DOC_ROOT + "guides/rollouts.md"], 9: [docs.CODE_REF, move_pin.PAGE]}
        decision = self.decide()
        self.assertFalse(decision["needed"])
        self.assertIn("#9", decision["reason"])

    def test_a_closed_pull_request_declines_that_commit(self):
        self.closed = [{"number": 12}]
        decision = self.decide()
        self.assertFalse(decision["needed"])
        self.assertIn("#12", decision["reason"])

    def test_commit_subjects_stay_one_printable_line_without_comment_markers(self):
        self.comparison = comparison(messages=["Fix a bug\n\nBody\n```\n@someone", "",
                                               f"{docs.MARKER}{OLD}:area:concern -->\x1b[31m red", "x" * 300])
        commits = self.decide()["commits"]
        self.assertEqual(commits[:2], ["0000001 Fix a bug", "0000002"])
        self.assertNotIn("<!--", commits[2])
        self.assertNotIn("\x1b", commits[2])
        self.assertEqual(len(commits[3]), len("0000004 ") + 100)

    def test_a_long_range_lists_its_first_commits_and_counts_the_rest(self):
        self.comparison = comparison(messages=[f"Change {index}" for index in range(60)])
        decision = self.decide()
        self.assertEqual(len(decision["commits"]), move_pin.MAX_LISTED)
        body = move_pin.description(decision)
        self.assertIn("is 60 commits ahead", body)
        self.assertIn("\n0000032 Change 49\n... and 10 more\n```", body)
        self.comparison = comparison()
        self.assertIn("is 1 commit ahead", move_pin.description(self.decide()))


class PublicationTests(Repository):
    def setUp(self):
        super().setUp()
        remote = tempfile.TemporaryDirectory()
        self.addCleanup(remote.cleanup)
        subprocess.run(["git", "init", "--bare", "-q", remote.name], check=True)
        self.git("remote", "add", "origin", remote.name)
        # The generated page arrives as an artifact, outside the checkout.
        artifact = tempfile.TemporaryDirectory()
        self.addCleanup(artifact.cleanup)
        self.generated = Path(artifact.name) / "ome.v1beta1.md"
        self.generated.write_text(NEW_PAGE)

    def publish(self, dry_run=False):
        calls = []
        original = docs.run

        def fake_gh(*args):
            if args[:3] == ("gh", "pr", "create"):
                calls.append((args, Path(args[args.index("--body-file") + 1]).read_text()))
                return "https://github.com/test/repo/pull/1"
            return original(*args)

        with patch.object(docs, "run", side_effect=fake_gh), \
                patch.dict(os.environ, {"DRY_RUN": str(dry_run).lower()}):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            move_pin.publish("test/repo", self.base, "main", NEW, self.generated)
        return calls

    def assert_nothing_published(self):
        self.assertEqual(self.git("ls-remote", "--heads", "origin"), "")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_publish_makes_one_signed_off_commit_and_one_pr(self):
        (args, body), = self.publish()
        self.assertEqual(self.git("rev-parse", "HEAD^"), self.base)
        self.assertEqual(set(self.git("diff", "--name-only", self.base).splitlines()),
                         {docs.CODE_REF, move_pin.PAGE})
        self.assertEqual(Path(docs.CODE_REF).read_text(), NEW + "\n")
        self.assertEqual(Path(move_pin.PAGE).read_text(), NEW_PAGE)
        message = self.git("log", "-1", "--format=%B")
        self.assertEqual(message.splitlines()[0], TITLE)
        self.assertLessEqual(len(TITLE), 52)
        self.assertIn("Signed-off-by: github-actions[bot]", message)
        branch = move_pin.branch_for(NEW)
        self.assertTrue(self.git("ls-remote", "origin", branch).startswith(self.git("rev-parse", "HEAD")))
        self.assertEqual([args[args.index(flag) + 1] for flag in ("--base", "--head", "--title")],
                         ["main", branch, TITLE])
        self.assertIn(f"https://github.com/{docs.CODE_REPO}/compare/{OLD}...{NEW}", body)
        self.assertIn("```text\n0000001 Add a field (#1)\n```", body)
        # Maintenance and the nightly must never take a pin move for a documentation PR.
        self.assertNotIn(docs.MARKER, body)
        self.assertFalse(branch.startswith("codex/nightly-docs-"))

    def test_unchanged_api_types_move_only_the_pin(self):
        self.generated.write_text(OLD_PAGE)
        self.assertEqual(len(self.publish()), 1)
        self.assertEqual(self.git("diff", "--name-only", self.base), docs.CODE_REF)

    def test_dry_run_validates_without_a_branch_or_pr(self):
        self.assertEqual(self.publish(dry_run=True), [])
        self.assert_nothing_published()

    def test_publish_rechecks_the_decision_before_writing_anything(self):
        self.open, self.files = [{"number": 9}], {9: [docs.CODE_REF]}
        self.assertEqual(self.publish(), [])
        self.assert_nothing_published()
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_generated_page_is_imported_as_validated_data(self):
        def empty():
            self.generated.write_text("")

        def binary():
            self.generated.write_bytes(b"---\n\xff\xfe")

        def link():
            self.generated.unlink()
            self.generated.symlink_to(Path(docs.CODE_REF).resolve())

        for name, spoil in [("empty", empty), ("binary", binary), ("link", link),
                            ("missing", self.generated.unlink),
                            ("nul", lambda: self.generated.write_text("---\n\x00")),
                            ("no front matter", lambda: self.generated.write_text("# OME API\n"))]:
            with self.subTest(name=name):
                spoil()
                with self.assertRaises(ValueError):
                    self.publish()
                self.assert_nothing_published()
                self.assertEqual(self.git("status", "--porcelain"), "")
        self.generated.write_text(NEW_PAGE)
        with patch.object(move_pin, "MAX_PAGE_BYTES", len(NEW_PAGE) - 1), \
                self.assertRaisesRegex(ValueError, "exceeds"):
            self.publish()

    def test_only_a_clean_checkout_of_the_baseline_is_published(self):
        Path("unexpected.md").write_text("Unrelated\n")
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            self.publish()
        Path("unexpected.md").unlink()
        Path(move_pin.PAGE).write_text("Edited by hand\n")
        self.git("commit", "-qam", "unexpected commit")
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            self.publish()
        self.assertEqual(self.git("ls-remote", "--heads", "origin"), "")

    def test_retry_recovers_matching_branch_without_rewriting_it(self):
        self.publish()
        published = self.git("rev-parse", "HEAD")
        self.git("switch", "--detach", self.base)
        self.assertEqual(len(self.publish()), 1)
        self.assertTrue(self.git("ls-remote", "origin", move_pin.branch_for(NEW)).startswith(published))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_retry_never_overwrites_a_different_existing_branch(self):
        branch = move_pin.branch_for(NEW)
        self.git("push", "origin", f"HEAD:refs/heads/{branch}")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.publish()
        self.assertTrue(self.git("ls-remote", "origin", branch).startswith(self.base))

    def test_git_publication_never_executes_hooks(self):
        marker = Path(self.temp.name) / "hook-ran"
        for name in ["pre-commit", "post-checkout", "pre-push"]:
            hook = Path(".git/hooks") / name
            hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
            hook.chmod(0o755)
        self.assertEqual(len(self.publish()), 1)
        self.assertFalse(marker.exists())


class WorkflowTests(unittest.TestCase):
    def test_only_the_publisher_holds_a_write_token_and_it_runs_no_ome_code(self):
        import yaml
        root = Path(__file__).resolve().parents[2]
        workflow = yaml.load((root / ".github/workflows/move-ome-pin.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["permissions"], {"contents": "read", "pull-requests": "read"})
        generate, publisher = workflow["jobs"]["generate"], workflow["jobs"]["publish"]
        self.assertIn("github.repository == 'ome-projects/ome-docs'", generate["if"])
        self.assertNotIn("permissions", generate)
        self.assertNotIn("GH_TOKEN", generate.get("env", {}))
        self.assertEqual([step["name"] for step in generate["steps"] if "GH_TOKEN" in step.get("env", {})],
                         ["Compare the pin with OME's newest commit"])
        self.assertEqual(publisher["permissions"], {"contents": "write", "pull-requests": "write"})
        self.assertNotIn("GH_TOKEN", publisher.get("env", {}))
        for step in publisher["steps"]:
            self.assertNotIn("repository", step.get("with", {}))
            self.assertFalse(step.get("uses", "").startswith("actions/setup-go@"))
            self.assertNotRegex(step.get("run", ""), r"\b(make|go|genref)\b")
            if "GH_TOKEN" in step.get("env", {}):
                self.assertEqual(step["name"], "Open the pull request that moves the pin")


if __name__ == "__main__":
    unittest.main()
