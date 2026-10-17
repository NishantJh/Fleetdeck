"""Keep the owner's local machine paths out of the repository."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Windows user/profile paths, WSL drive mounts, macOS home folders, and the owner's own folders.
CONTENT_PATTERNS = [
    re.compile(r"[A-Za-z]:[\/]+(Users|Documents and Settings)[\/]", re.I),
    re.compile(r"/mnt/[a-z]/"),
    re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    re.compile(r"/home/nishant\b", re.I),
    re.compile(r"Claude projects", re.I),
    re.compile(r"AppData[\/]Local", re.I),
]
# Files that legitimately mention these strings.
SKIP = {"tests/test_repo_hygiene.py", "LICENSE", "requirements.txt"}
# Folders named after a Windows path whose backslashes were lost, e.g. "C:UsersNameAppData...".
BAD_NAME = re.compile(r"^[A-Za-z][:\uf03a]|Users[^/]*AppData", re.I)


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def test_no_local_paths_in_file_names():
    bad = [p for p in _tracked() if BAD_NAME.search(p)]
    assert not bad, f"tracked files named after a local path: {bad[:5]}"


def test_no_local_paths_in_file_contents():
    hits = []
    for rel in _tracked():
        if rel in SKIP:
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary files (screenshots) or removed paths
        for n, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in CONTENT_PATTERNS):
                hits.append(f"{rel}:{n}")
    assert not hits, f"local machine paths found: {hits[:10]}"
