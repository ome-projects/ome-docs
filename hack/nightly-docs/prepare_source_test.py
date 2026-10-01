import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import prepare_source


class PrepareSourceTests(unittest.TestCase):
    def test_branch_tools_survive_checkout_and_doc_commit_has_only_docs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()

            def git(*args):
                return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args],
                                               cwd=repo, text=True).strip()

            git("init", "-q")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            scripts = repo / "hack/nightly-docs"
            scripts.mkdir(parents=True)
            script = scripts / "prepare_source.py"
            script.write_text(Path(prepare_source.__file__).read_text())
            (scripts / "version").write_text("main tooling")
            (repo / "doc.md").write_text("main docs")
            git("add", ".")
            git("commit", "-qm", "main")
            base = git("rev-parse", "HEAD")
            git("switch", "-qc", "fix")
            (scripts / "version").write_text("fixed branch tooling")
            git("commit", "-qam", "fix tooling")
            destination = root / "trusted-tools"
            subprocess.run([os.sys.executable, str(script), base, str(destination)],
                           cwd=repo, check=True, capture_output=True)
            self.assertEqual(git("rev-parse", "HEAD"), base)
            self.assertEqual((destination / "version").read_text(), "fixed branch tooling")
            self.assertEqual((scripts / "version").read_text(), "main tooling")
            (repo / "doc.md").write_text("updated docs")
            git("add", "doc.md")
            git("commit", "-qm", "docs")
            self.assertEqual(git("rev-parse", "HEAD^"), base)
            self.assertEqual(git("diff", "--name-only", base), "doc.md")

    def test_rejects_unpinned_source(self):
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            prepare_source.prepare("main", "unused")

    def repository(self, root, name):
        path = root / name
        path.mkdir()

        def git(*args):
            return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", *args],
                                           cwd=path, text=True).strip()

        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        return path, git

    def documentation(self, root, pin):
        repo, git = self.repository(root, "repo")
        scripts = repo / "hack/nightly-docs"
        scripts.mkdir(parents=True)
        (scripts / "prepare_source.py").write_text(Path(prepare_source.__file__).read_text())
        (repo / prepare_source.CODE_REF).write_text(pin + "\n")
        git("add", ".")
        git("commit", "-qm", "main")
        return repo, git, scripts / "prepare_source.py"

    def test_code_is_cloned_outside_the_docs_tree_at_the_commit_the_source_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ome, ome_git = self.repository(root, "ome")
            (ome / "source.go").write_text("package pinned\n")
            ome_git("add", ".")
            ome_git("commit", "-qm", "pinned")
            pinned = ome_git("rev-parse", "HEAD")
            (ome / "source.go").write_text("package newer\n")
            ome_git("commit", "-qam", "newer")
            repo, git, script = self.documentation(root, pinned)
            base = git("rev-parse", "HEAD")
            # A later documentation revision moves the pin; the source revision decides.
            (repo / prepare_source.CODE_REF).write_text(ome_git("rev-parse", "HEAD") + "\n")
            git("commit", "-qam", "move the pin")
            code = root / "code"
            subprocess.run([os.sys.executable, str(script), base, str(root / "tools"), str(code), str(ome)],
                           cwd=repo, check=True, capture_output=True)

            def code_git(*args):
                return subprocess.check_output(["git", "-C", str(code), *args], text=True).strip()

            self.assertEqual(code_git("rev-parse", "HEAD"), pinned)
            self.assertEqual((code / "source.go").read_text(), "package pinned\n")
            # The full history stays available for ancestry checks and later pins.
            self.assertEqual(len(code_git("rev-list", "--all").splitlines()), 2)
            self.assertEqual(git("rev-parse", "HEAD"), base)
            self.assertEqual(git("status", "--porcelain"), "")

    def test_rejects_a_pin_that_is_not_a_full_commit_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, git, script = self.documentation(root, "main")
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                subprocess.run([os.sys.executable, str(script), git("rev-parse", "HEAD"),
                                str(root / "tools"), str(root / "code"), str(root / "unused")],
                               cwd=repo, check=True, capture_output=True)
            self.assertIn(b"full OME commit SHA", failure.exception.stderr)
            self.assertFalse((root / "code").exists())


if __name__ == "__main__":
    unittest.main()
