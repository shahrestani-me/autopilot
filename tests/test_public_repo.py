"""This repository is public: no private data in it (AGENTS.md, "Public repository").

A coarse net for the common slips: real e-mail addresses and home-folder paths. It can't know a
private host or project name, so the rule in AGENTS.md is what keeps those out.
"""

import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
EMAIL_DOMAINS = {"example.com", "example.org", "github.com", "users.noreply.github.com", "anthropic.com"}
HOME = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)")
HOME_NAMES = {"me", "you", "runner"}


def _tracked_text_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    for name in out.splitlines():
        path = ROOT / name
        try:
            yield name, path.read_text()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue


class PublicRepoTests(unittest.TestCase):
    def test_no_private_emails_or_home_paths(self):
        found = []
        for name, text in _tracked_text_files():
            for lineno, line in enumerate(text.splitlines(), 1):
                for m in EMAIL.finditer(line):
                    if m.group(1).lower() not in EMAIL_DOMAINS:
                        found.append(f"{name}:{lineno}: {m.group(0)}")
                for m in HOME.finditer(line):
                    if m.group(1) not in HOME_NAMES:
                        found.append(f"{name}:{lineno}: {m.group(0)}")
        self.assertEqual(found, [], "use example.com addresses and ~/ or /Users/me paths")


if __name__ == "__main__":
    unittest.main()
