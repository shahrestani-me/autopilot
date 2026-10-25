"""Unit tests for bin/autopilot-clobber: the base guard. Stdlib only; builds throwaway git repos."""

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


T0 = "2026-09-14T08:00:00+00:00"   # base before anyone branches
T_OPEN = "2026-09-14T08:38:00+00:00"  # the stale PR is opened
T_OTHER = "2026-09-14T10:08:00+00:00"  # another PR lands on main
T_LATE = "2026-09-14T10:53:00+00:00"   # the stale PR is rewritten on top of main


class Repo:
    def __init__(self, path):
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def git(self, *args, when=None):
        env = dict(os.environ)
        if when:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
        return subprocess.run(["git", "-C", self.path, *args], capture_output=True, text=True,
                              check=True, env=env).stdout.strip()

    def write(self, rel, text):
        p = pathlib.Path(self.path, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg, when):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg, when=when)
        return self.git("rev-parse", "HEAD")


class ClobberTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = _load("autopilot-clobber")
        self.r = Repo(self.tmp.name)
        self.r.write("auth.py", "def login():\n    return 1\n")
        self.r.write("ci.yml", "jobs: {}\n")
        self.base = self.r.commit("base", T0)
        # Another PR lands on main after the stale PR was opened: new code and a new test file.
        self.r.write("auth.py", "def login():\n    return 1\n\n\ndef reclaim():\n    return 'verified'\n")
        self.r.write("test_reclaim.py", "def test_reclaim():\n    assert True\n")
        self.other = self.r.commit("feat: reclaim (#433)", T_OTHER)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, head, since=T_OPEN, allow=()):
        return self.c.find_clobbers(self.r.path, self.r.git("merge-base", "main", head), head,
                                    self.c.epoch(since), allow)

    def stale_branch(self):
        """The #432 shape: a commit on top of main whose files are the pre-#433 copies plus its own change."""
        self.r.git("checkout", "-q", "-b", "stale", self.other)
        self.r.git("checkout", self.base, "--", "auth.py")
        self.r.git("rm", "-q", "test_reclaim.py")
        self.r.write("ci.yml", "jobs: {gitleaks: {}}\n")
        return self.r.commit("ci: add gitleaks (#432)", T_LATE)

    def honest_branch(self):
        """The same CI change made properly on top of main."""
        self.r.git("checkout", "-q", "-b", "honest", self.other)
        self.r.write("ci.yml", "jobs: {gitleaks: {}}\n")
        return self.r.commit("ci: add gitleaks", T_LATE)

    def test_the_stale_overwrite_is_caught_and_names_the_victim(self):
        hits = self.check(self.stale_branch())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["origin"], self.other)
        self.assertEqual(hits[0]["summary"], "feat: reclaim (#433)")
        # reclaim()'s two code lines; the two blank lines above it are nobody's work.
        self.assertEqual(hits[0]["files"], {"auth.py": 2, "test_reclaim.py": 2})
        self.assertEqual(hits[0]["deleted_files"], ["test_reclaim.py"])

    def test_an_honest_change_on_top_of_main_is_clean(self):
        self.assertEqual(self.check(self.honest_branch()), [])

    def test_deleting_lines_older_than_the_pr_is_a_normal_edit(self):
        self.r.git("checkout", "-q", "-b", "edit", self.other)
        self.r.write("auth.py", "def login():\n    return 2\n\n\ndef reclaim():\n    return 'verified'\n")
        head = self.r.commit("fix: login returns 2", T_LATE)
        self.assertEqual(self.check(head), [])

    def test_a_pr_started_after_the_other_landed_may_change_its_lines(self):
        # Opened after #433 merged: its author saw reclaim() and chose to remove it.
        self.assertEqual(self.check(self.stale_branch(), since=T_LATE), [])

    def test_an_intended_supersede_is_allowed_by_origin_prefix(self):
        self.assertEqual(self.check(self.stale_branch(), allow=(self.other[:7],)), [])

    def test_the_cli_exits_3_with_json_and_0_when_clean(self):
        stale = self.stale_branch()
        self.r.git("checkout", "-q", "main")
        honest = self.honest_branch()
        out = subprocess.run([str(ROOT / "bin" / "autopilot-clobber"), "check", "--repo-dir", self.r.path,
                              "--base", "main", "--head", stale, "--since", T_OPEN], capture_output=True, text=True)
        self.assertEqual(out.returncode, 3, out.stderr)
        body = json.loads(out.stdout)
        self.assertFalse(body["ok"])
        self.assertEqual(body["clobbers"][0]["origin"], self.other)
        ok = subprocess.run([str(ROOT / "bin" / "autopilot-clobber"), "check", "--repo-dir", self.r.path,
                             "--base", "main", "--head", honest, "--since", T_OPEN], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertTrue(json.loads(ok.stdout)["ok"])

    def test_check_without_a_since_or_pr_is_a_usage_error_not_a_pass(self):
        out = subprocess.run([str(ROOT / "bin" / "autopilot-clobber"), "check", "--repo-dir", self.r.path,
                              "--base", "main", "--head", "main"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)

    def test_dropping_only_a_newer_blank_line_is_not_a_clobber(self):
        self.r.git("checkout", "-q", "-b", "tidy", self.other)
        self.r.write("auth.py", "def login():\n    return 1\n\ndef reclaim():\n    return 'verified'\n")
        self.assertEqual(self.check(self.r.commit("style: one blank line", T_LATE)), [])

    def test_a_deleted_binary_file_from_a_newer_change_is_caught(self):
        self.r.git("checkout", "-q", "main")
        pathlib.Path(self.r.path, "fixture.pdf").write_bytes(b"%PDF-1.4\x00\x01\x02binary")
        pdf = self.r.commit("test: add a pdf fixture", "2026-09-14T10:30:00+00:00")
        self.r.git("checkout", "-q", "-b", "drop-pdf", pdf)
        self.r.git("rm", "-q", "fixture.pdf")
        hits = self.check(self.r.commit("chore: stale copy", T_LATE))
        self.assertEqual([h["origin"] for h in hits], [pdf])
        self.assertEqual(hits[0]["deleted_files"], ["fixture.pdf"])

    def test_moving_a_file_with_newer_lines_intact_is_not_a_clobber(self):
        self.r.git("checkout", "-q", "-b", "move", self.other)
        self.r.git("mv", "auth.py", "accounts.py")
        self.assertEqual(self.check(self.r.commit("refactor: rename auth.py", T_LATE)), [])

    def test_paths_git_quotes_still_reach_blame(self):
        names = ["caf\u00e9.md", 'say "hi".md', "back\\slash.md"]
        self.r.git("checkout", "-q", "main")
        for n in names:
            self.r.write(n, "kept\nnewer work\n")
        added = self.r.commit("docs: odd names", "2026-09-14T10:30:00+00:00")
        self.r.git("checkout", "-q", "-b", "odd", added)
        for n in names:
            self.r.write(n, "kept\n")
        hits = self.check(self.r.commit("chore: stale copies", T_LATE))
        self.assertEqual([h["origin"] for h in hits], [added])
        self.assertEqual(sorted(hits[0]["files"]), sorted(names))

    def test_git_path_unquotes_c_escapes(self):
        self.assertEqual(self.c.git_path('"a/caf\\303\\251 \\"x\\".md"', "a/"), 'caf\u00e9 "x".md')
        self.assertEqual(self.c.git_path("a/plain.py", "a/"), "plain.py")

    def test_a_stale_copy_that_brings_back_removed_code_is_caught(self):
        self.r.git("checkout", "-q", "main")
        self.r.write("legacy.py", "def insecure_endpoint():\n    return run_shell(request.args)\n")
        self.r.commit("old: legacy endpoint", T0)
        stale_base = self.r.git("rev-parse", "HEAD")
        self.r.git("rm", "-q", "legacy.py")
        removal = self.r.commit("security: drop the legacy endpoint", "2026-09-14T10:30:00+00:00")
        # Branch opened before the removal, rewritten on top of main from a stale copy.
        self.r.git("checkout", "-q", "-b", "zombie", removal)
        self.r.git("checkout", stale_base, "--", "legacy.py")
        self.r.write("ci.yml", "jobs: {gitleaks: {}}\n")
        hits = self.check(self.r.commit("ci: gitleaks", T_LATE))
        self.assertEqual([h["origin"] for h in hits], [removal])
        self.assertEqual(hits[0]["resurrected"], {"legacy.py": 2})

    def test_re_adding_a_line_that_still_exists_elsewhere_in_the_file_is_not_a_resurrection(self):
        self.r.git("checkout", "-q", "main")
        self.r.write("auth.py", "def login():\n    return 1\n\n\ndef reclaim():\n    return 'verified'\n\n\ndef other():\n    return 1\n")
        self.r.commit("feat: other", "2026-09-14T10:20:00+00:00")
        self.r.write("auth.py", "def login():\n    return 1\n\n\ndef reclaim():\n    return 'verified'\n")
        self.r.commit("refactor: drop other()", "2026-09-14T10:40:00+00:00")
        self.r.git("checkout", "-q", "-b", "dup")
        self.r.write("auth.py", "def login():\n    return 1\n\n\ndef reclaim():\n    return 'verified'\n\n\ndef login2():\n    return 1\n")
        self.assertEqual(self.check(self.r.commit("feat: login2", T_LATE)), [])

    def test_re_adding_one_line_a_newer_change_dropped_is_a_normal_edit(self):
        self.r.git("checkout", "-q", "main")
        self.r.write("util.py", "import os\nimport sys\n\nprint(sys.argv)\n")
        self.r.commit("util", T0)
        self.r.write("util.py", "import sys\n\nprint(sys.argv)\n")
        dropped = self.r.commit("chore: drop unused import os", "2026-09-14T10:30:00+00:00")
        self.r.git("checkout", "-q", "-b", "needs-os", dropped)
        self.r.write("util.py", "import sys\nimport os\n\nprint(sys.argv, os.sep)\n")
        self.assertEqual(self.check(self.r.commit("feat: print the separator", T_LATE)), [])

    def test_a_binary_rewritten_back_to_an_older_version_is_caught(self):
        self.r.git("checkout", "-q", "main")
        pdf = pathlib.Path(self.r.path, "fixture.pdf")
        pdf.write_bytes(b"%PDF-1.4\x00old")
        self.r.commit("test: pdf v1", T0)
        v1 = pdf.read_bytes()
        pdf.write_bytes(b"%PDF-1.4\x00new case")
        v2 = self.r.commit("test: pdf v2 for the new parser case", "2026-09-14T10:30:00+00:00")
        self.r.git("checkout", "-q", "-b", "oldpdf", v2)
        pdf.write_bytes(v1)
        hits = self.check(self.r.commit("chore: stale copy", T_LATE))
        self.assertEqual([h["origin"] for h in hits], [v2])
        self.assertEqual(hits[0]["deleted_files"], [])

    def test_moving_a_newer_binary_is_not_a_clobber_whatever_diff_renames_says(self):
        self.r.git("config", "diff.renames", "false")
        self.r.git("checkout", "-q", "main")
        pathlib.Path(self.r.path, "logo.png").write_bytes(b"\x89PNG\x00\x01\x02")
        logo = self.r.commit("assets: logo", "2026-09-14T10:30:00+00:00")
        self.r.git("checkout", "-q", "-b", "mvlogo", logo)
        pathlib.Path(self.r.path, "brand").mkdir()
        self.r.git("mv", "logo.png", "brand/logo.png")
        self.assertEqual(self.check(self.r.commit("assets: move logo", T_LATE)), [])

    def test_a_configured_blame_ignore_revs_file_cannot_hide_the_origin(self):
        # An ignored commit that *rewrites* a line hands blame to the older commit before it,
        # which predates the PR, so the stale revert would pass if the config were honored.
        self.r.git("checkout", "-q", "main")
        self.r.write("ci.yml", "jobs: {lint: {}}\n")
        rewrite = self.r.commit("ci: lint job", "2026-09-14T10:30:00+00:00")
        pathlib.Path(self.r.path, ".git-blame-ignore-revs").write_text(rewrite + "\n")
        self.r.git("config", "blame.ignoreRevsFile", ".git-blame-ignore-revs")
        self.r.git("checkout", "-q", "-b", "oldci", rewrite)
        self.r.write("ci.yml", "")  # deletes the line and adds nothing, so only blame can see it
        hits = self.check(self.r.commit("chore: stale ci", T_LATE))
        self.assertEqual([h["origin"] for h in hits], [rewrite])

    def test_adding_a_new_binary_is_fine(self):
        self.r.git("checkout", "-q", "-b", "newpng", self.other)
        pathlib.Path(self.r.path, "icon.png").write_bytes(b"\x89PNG\x00\x01")
        self.assertEqual(self.check(self.r.commit("assets: icon", T_LATE)), [])

    def test_a_naive_since_is_utc_not_local_time(self):
        self.assertEqual(self.c.epoch("2026-09-14T08:38:05"), self.c.epoch("2026-09-14T08:38:05Z"))

    def test_a_head_behind_its_base_exits_4_unless_allowed(self):
        self.r.git("checkout", "-q", "-b", "old", self.base)
        self.r.write("ci.yml", "jobs: {gitleaks: {}}\n")
        old = self.r.commit("ci: gitleaks on an old base", T_LATE)
        cli = [str(ROOT / "bin" / "autopilot-clobber"), "check", "--repo-dir", self.r.path,
               "--base", "main", "--head", old, "--since", T_OPEN]
        out = subprocess.run(cli, capture_output=True, text=True)
        self.assertEqual(out.returncode, 4, out.stderr)
        self.assertTrue(json.loads(out.stdout)["behind"])
        self.assertEqual(subprocess.run(cli + ["--allow-behind"], capture_output=True, text=True).returncode, 0)

    def test_pr_since_takes_the_earliest_of_creation_and_first_commit(self):
        pr = {"createdAt": "2026-09-14T08:38:05Z",
              "commits": [{"authoredDate": "2026-09-14T10:53:55Z", "committedDate": "2026-09-14T10:53:55Z"}]}
        self.assertEqual(self.c.pr_since(pr), self.c.epoch("2026-09-14T08:38:05Z"))


if __name__ == "__main__":
    unittest.main()
