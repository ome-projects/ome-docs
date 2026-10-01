"""Keep workflow-revision tooling while documenting a pinned main revision."""

from pathlib import Path
import re
import shutil
import subprocess
import sys

CODE_URL = "https://github.com/ome-projects/ome.git"
CODE_REF = "ome.ref"


def prepare(source_sha, destination, code_destination=None, code_url=CODE_URL):
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("Documentation source must be a full commit SHA")
    subprocess.run(["git", "cat-file", "-e", f"{source_sha}^{{commit}}"], check=True)
    shutil.copytree(Path(__file__).resolve().parent, destination)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "checkout", "--detach", source_sha], check=True)
    if code_destination:
        # The documentation revision, not the workflow revision, chooses the
        # OME commit, so read the pin only after the checkout above. The clone
        # stays outside the documentation tree and keeps OME's full history.
        code_sha = Path(CODE_REF).read_text().strip()
        if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
            raise ValueError(f"{CODE_REF} must hold one full OME commit SHA")
        git = ["git", "-c", "core.hooksPath=/dev/null"]
        subprocess.run([*git, "clone", "--quiet", "--no-checkout", code_url, code_destination], check=True)
        subprocess.run([*git, "-C", code_destination, "checkout", "--quiet", "--detach", code_sha], check=True)


if __name__ == "__main__":
    prepare(*sys.argv[1:5])
