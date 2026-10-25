"""Unit tests for bin/autopilot-inflight. Stdlib only; gh is faked."""

import datetime
import importlib.machinery
import os
import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("autopilot_inflight", str(ROOT / "bin" / "autopilot-inflight"))
_spec = importlib.util.spec_from_loader("autopilot_inflight", _loader)
api = importlib.util.module_from_spec(_spec)
_loader.exec_module(api)

API, APP = "o/api", "o/app"


def check(name, conclusion="SUCCESS", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


def pr(number, issue, *, checks=None, draft=False, mergeable="MERGEABLE", head="h", created="2026-10-19T21:00:00Z"):
    return {"number": number, "url": f"https://x/{number}", "title": f"pr {number}",
            "body": f"stuff\n\nPart of {issue} — https://linear/{issue}" if issue else "no link",
            "headRefName": f"b{number}", "headRefOid": head, "baseRefName": "main", "isDraft": draft,
            "mergeable": mergeable, "statusCheckRollup": checks if checks is not None else [check("test")],
            "createdAt": created}


class FakeGh:
    def __init__(self, prs_by_repo, base="base1"):
        self.prs, self.base, self.calls = prs_by_repo, base, []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["pr", "list"]:
            assert "--author" in args and args[args.index("--author") + 1] == "app/claude-autopilot"
            return json.dumps(self.prs.get(args[args.index("-R") + 1], []))
        if args[0] == "api":
            return self.base + "\n"
        raise AssertionError(args)


class InflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        api.STATE_DIR = self.tmp
        api.MAX_RUNS = 1  # the one-at-a-time rules; ParallelTests runs with 3

    def sweep(self, gh):
        return api.sweep([API, APP], gh=gh)

    def test_nothing_open_leaves_the_slot_free(self):
        out = self.sweep(FakeGh({}))
        self.assertEqual(out, {"capacity": 1, "max_runs": 1, "slot_taken": False, "next": None, "deferred_next": None,
                               "running": [], "stalled": [], "inflight": [], "waiting_no_pr": []})

    def test_green_prs_of_one_issue_are_grouped_and_finished_first(self):
        gh = FakeGh({API: [pr(513, "ENG-39")], APP: [pr(354, "ENG-39")]})
        out = self.sweep(gh)
        self.assertTrue(out["slot_taken"])
        self.assertEqual(len(out["inflight"]), 1)
        self.assertEqual(out["next"]["issue"], "ENG-39")
        self.assertEqual(out["next"]["action"], "finish")
        self.assertEqual([p["number"] for p in out["next"]["prs"]], [513, 354])  # by repo: o/api, o/app

    def test_a_red_pr_holds_the_whole_issue_and_is_fixed(self):
        gh = FakeGh({API: [pr(513, "ENG-39", checks=[check("test", "FAILURE"), check("lint")])],
                     APP: [pr(354, "ENG-39")]})
        item = self.sweep(gh)["next"]
        self.assertEqual((item["status"], item["action"]), ("ci-red", "fix-ci"))
        self.assertEqual(item["prs"][0]["failing"], ["test"])

    def test_finish_beats_rebase_beats_fix_ci_then_oldest(self):
        gh = FakeGh({API: [pr(1, "ENG-1", checks=[check("t", "FAILURE")], created="2026-10-01T00:00:00Z"),
                           pr(2, "ENG-2", mergeable="CONFLICTING", created="2026-10-02T00:00:00Z"),
                           pr(3, "ENG-3", created="2026-10-03T00:00:00Z")]})
        out = self.sweep(gh)
        self.assertEqual([i["issue"] for i in out["inflight"]], ["ENG-3", "ENG-2", "ENG-1"])

    def test_pending_checks_and_drafts_hold_the_slot_but_are_not_actionable(self):
        gh = FakeGh({API: [pr(1, "ENG-1", checks=[check("t", None, "IN_PROGRESS")]), pr(2, "ENG-2", draft=True)]})
        out = self.sweep(gh)
        self.assertTrue(out["slot_taken"])
        self.assertIsNone(out["next"])
        self.assertEqual({i["issue"]: i["status"] for i in out["inflight"]}, {"ENG-1": "pending", "ENG-2": "held-draft"})

    def test_unknown_mergeability_waits(self):
        out = self.sweep(FakeGh({API: [pr(1, "ENG-1", mergeable="UNKNOWN")]}))
        self.assertEqual(out["inflight"][0]["status"], "pending")

    def test_status_contexts_count(self):
        ctx = [{"__typename": "StatusContext", "context": "ci/legacy", "state": "ERROR"}]
        self.assertEqual(api.checks_state(ctx), ("red", ["ci/legacy"]))
        self.assertEqual(api.checks_state([{"__typename": "StatusContext", "context": "x", "state": "PENDING"}])[0], "pending")
        self.assertEqual(api.checks_state([check("a", "SKIPPED"), check("b", "NEUTRAL")])[0], "green")

    def test_a_pr_without_a_linked_issue_holds_the_slot_but_is_never_picked(self):
        out = self.sweep(FakeGh({API: [pr(9, None)]}))
        self.assertTrue(out["slot_taken"])
        self.assertIsNone(out["next"])
        self.assertIsNone(out["inflight"][0]["issue"])

    def test_a_block_stays_put_until_something_changes(self):
        prs = {API: [pr(513, "ENG-39", checks=[check("claude-review", "FAILURE")])]}
        api.wait("ENG-39", "checkable", "claude-review refuses bots", [API, APP], gh=FakeGh(prs))
        item = self.sweep(FakeGh(prs))["inflight"][0]
        self.assertFalse(item["actionable"])
        self.assertIn("nothing changed", item["reason"])
        # The owner fixes the workflow on main: the base moves, so the next run tries again.
        moved = self.sweep(FakeGh(prs, base="base2"))
        self.assertEqual(moved["next"]["issue"], "ENG-39")
        self.assertIn("changed", moved["next"]["reason"])

    def test_a_rerun_that_goes_green_unblocks(self):
        red = {API: [pr(513, "ENG-39", checks=[check("claude-review", "FAILURE")])]}
        api.wait("ENG-39", "checkable", "x", [API], gh=FakeGh(red))
        out = self.sweep(FakeGh({API: [pr(513, "ENG-39")]}))
        self.assertEqual(out["next"]["action"], "finish")

    def test_waiting_for_the_owner_never_resumes_on_its_own(self):
        prs = {API: [pr(1, "ENG-1")]}
        api.wait("ENG-1", "owner", "which option?", [API], gh=FakeGh(prs))
        out = self.sweep(FakeGh(prs, base="moved"))
        self.assertTrue(out["slot_taken"])
        self.assertIsNone(out["next"])
        self.assertIn("owner", out["inflight"][0]["reason"])

    def test_clear_and_blocks_without_a_pr(self):
        api.wait("ENG-7", "owner", "pick a, b or c", [API], gh=FakeGh({}))
        out = self.sweep(FakeGh({}))
        self.assertFalse(out["slot_taken"])  # a question with no code doesn't hold the slot
        self.assertEqual(out["waiting_no_pr"][0]["issue"], "ENG-7")
        self.assertNotIn("fingerprint", out["waiting_no_pr"][0])
        self.assertEqual(api.clear("ENG-7"), {"cleared": "ENG-7"})
        self.assertEqual(api.clear("ENG-7"), {"cleared": None})
        self.assertEqual(self.sweep(FakeGh({}))["waiting_no_pr"], [])

    def test_new_work_asked_for_mid_flight_runs_once_the_slot_frees(self):
        busy = FakeGh({API: [pr(1, "ENG-1")]})
        api.wait("ENG-9", "slot", "owner asked for X while ENG-1 was in flight", [API], gh=busy)
        api.wait("ENG-8", "owner", "a question", [API], gh=busy)
        self.assertIsNone(self.sweep(busy)["deferred_next"])  # ENG-1 still open
        free = self.sweep(FakeGh({}))
        self.assertEqual(free["deferred_next"]["issue"], "ENG-9")  # never the question

    def test_a_claude_wait_is_offered_to_claude_runs_only(self):
        api.wait("ENG-5", "claude", "merged; preprod check waits for a Claude run", [API], gh=FakeGh({}))
        self.assertEqual(self.sweep(FakeGh({}))["deferred_next"]["issue"], "ENG-5")
        os.environ["AUTOPILOT_FALLBACK"] = "1"
        try:
            self.assertIsNone(self.sweep(FakeGh({}))["deferred_next"])
        finally:
            del os.environ["AUTOPILOT_FALLBACK"]

    def test_base_sha_is_fetched_once_per_repo_and_branch(self):
        gh = FakeGh({API: [pr(1, "ENG-1"), pr(2, "ENG-2")]})
        self.sweep(gh)
        self.assertEqual(sum(1 for c in gh.calls if c[0] == "api"), 1)

    def test_automatic_retries_are_capped_then_the_owner_is_told_once(self):
        red = lambda head: {API: [pr(1, "ENG-1", head=head, checks=[check("t", "FAILURE")])]}
        api.wait("ENG-1", "checkable", "red", [API], gh=FakeGh(red("a")))
        for n, head in enumerate(("b", "c"), start=1):
            self.assertEqual(self.sweep(FakeGh(red(head)))["next"]["issue"], "ENG-1")  # something changed
            api.claim("ENG-1")
            api.wait("ENG-1", "checkable", "still red", [API], gh=FakeGh(red(head)))
            self.assertEqual(api.read_waiting("ENG-1")["auto_retries"], n)
        out = self.sweep(FakeGh(red("d")))  # changed again, but twice is enough
        self.assertIsNone(out["next"])
        self.assertEqual([i["issue"] for i in out["stalled"]], ["ENG-1"])
        api.notified("ENG-1", out["stalled"][0]["reason"])
        self.assertEqual(self.sweep(FakeGh(red("e")))["stalled"], [])  # told once
        api.clear("ENG-1")  # the owner answered: a fresh start
        self.assertEqual(self.sweep(FakeGh(red("f")))["next"]["issue"], "ENG-1")

    def test_a_crash_after_claim_counts_too(self):
        prs = {API: [pr(1, "ENG-1", checks=[check("t", "FAILURE")])]}
        api.claim("ENG-1")
        api.claim("ENG-1")  # two runs died mid-fix, no block recorded
        self.assertEqual([i["issue"] for i in self.sweep(FakeGh(prs))["stalled"]], ["ENG-1"])

    def test_checks_pending_for_hours_are_reported_as_stalled(self):
        prs = {API: [pr(1, "ENG-1", checks=[{"__typename": "StatusContext", "context": "ext", "state": "EXPECTED"}])]}
        t0 = datetime.datetime(2026, 10, 20, 8, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(api.sweep([API], gh=FakeGh(prs), now=t0)["stalled"], [])
        later = api.sweep([API], gh=FakeGh(prs), now=t0 + datetime.timedelta(hours=api.STALL_HOURS))
        self.assertEqual(later["stalled"][0]["issue"], "ENG-1")
        self.assertIn("2026-10-20T08:00:00Z", later["stalled"][0]["reason"])
        # Once green, the clock is forgotten.
        api.sweep([API], gh=FakeGh({API: [pr(1, "ENG-1")]}), now=t0 + datetime.timedelta(hours=3))
        again = api.sweep([API], gh=FakeGh(prs), now=t0 + datetime.timedelta(hours=4))
        self.assertEqual(again["stalled"], [])

    def test_a_block_ends_when_its_prs_are_gone(self):
        api.wait("ENG-1", "owner", "q", [API], gh=FakeGh({API: [pr(1, "ENG-1")]}))
        self.sweep(FakeGh({}))  # the owner merged it by hand
        self.assertIsNone(api.read_waiting("ENG-1"))
        api.wait("ENG-3", "owner", "q", [APP], gh=FakeGh({APP: [pr(5, "ENG-3")]}))
        api.sweep([API], gh=FakeGh({}))  # o/app not swept this time (its preprod is broken)
        self.assertIsNotNone(api.read_waiting("ENG-3"))
        api.wait("ENG-2", "owner", "a question, no code", [API], gh=FakeGh({}))
        self.sweep(FakeGh({}))
        self.assertIsNotNone(api.read_waiting("ENG-2"))  # nothing recorded to land: keep it

    def test_repos_come_from_the_configs(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.yaml"), "w") as f:
            f.write("github: app\nrepos:\n  - name: x\n    github: o/x   # comment\n  - name: y\n    github: o/y\n")
        with open(os.path.join(d, "b.yaml"), "w") as f:
            f.write("repos:\n  - github: o/x\n")
        self.assertEqual(api.configured_repos(d), ["o/x", "o/y"])

    def test_count_wakes_a_run_only_for_work(self):
        self.assertEqual(api.count([API], gh=FakeGh({})), 0)
        self.assertEqual(api.count([API], gh=FakeGh({API: [pr(1, "ENG-1", checks=[check("t", None, "IN_PROGRESS")])]})), 0)
        self.assertEqual(api.count([API], gh=FakeGh({API: [pr(1, "ENG-1")]})), 1)

    def test_cli_rejects_a_bad_issue_id(self):
        with self.assertRaises(SystemExit):
            api.run(["clear", "../etc"], gh=FakeGh({}))


if __name__ == "__main__":
    unittest.main()


class ParallelTests(unittest.TestCase):
    """Up to three runs side by side (owner, 2026-10-20)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        api.STATE_DIR = self.tmp
        api.MAX_RUNS = 3

    def test_slots_cap_the_runs_and_free_up(self):
        got = [api.slot("take") for _ in range(4)]
        self.assertEqual([g["slot"] for g in got], [1, 2, 3, None])
        self.assertEqual(api.slot("busy"), 3)
        api.slot("release", 2, token=got[1]["token"])
        self.assertEqual(api.slot("take")["slot"], 2)

    def test_slot_take_echoes_the_agent_and_checks_its_name(self):
        self.assertEqual(api.run(["slot", "take", "--agent", "autopilot-agent-2"])["agent"], "autopilot-agent-2")
        self.assertNotIn("agent", api.run(["slot", "take"]))
        with self.assertRaises(SystemExit):
            api.run(["slot", "take", "--agent", "agent`$(x)"])

    def test_only_the_holder_can_touch_or_release(self):
        mine = api.slot("take")
        theirs = api.slot("take")
        with self.assertRaises(SystemExit):
            api.slot("release", 1, token=theirs["token"])  # wrong number for this token
        with self.assertRaises(SystemExit):
            api.slot("touch", 3)  # nobody holds slot 3
        api.slot("touch", 1, token=mine["token"])
        lk = api.lock("ENG-1")
        with self.assertRaises(SystemExit):
            api.unlock("ENG-1", "not-it")
        api.lock("ENG-1", touch=True, token=lk["token"])
        self.assertEqual(api.unlock("ENG-1", lk["token"]), {"unlocked": "ENG-1"})

    def test_one_taker_at_a_time_for_a_stale_lock(self):
        api.lock("ENG-1")
        os.utime(api._lock_path("ENG-1"), (1, 1))
        # Another run is mid-takeover: its marker is fresh, so this one backs off.
        open(api._lock_path("ENG-1") + ".takeover", "w").write("token=x\n")
        self.assertFalse(api.lock("ENG-1")["locked"])
        os.utime(api._lock_path("ENG-1") + ".takeover", (1, 1))  # that taker died
        self.assertTrue(api.lock("ENG-1")["locked"])
        self.assertFalse(os.path.exists(api._lock_path("ENG-1") + ".takeover"))

    def test_a_stale_slot_or_lock_is_taken_over(self):
        api.slot("take")
        path = api._slot_path(1)
        os.utime(path, (1, 1))  # a run that died long ago
        self.assertEqual(api.slot("take")["slot"], 1)
        self.assertTrue(api.lock("ENG-1")["locked"])
        os.utime(api._lock_path("ENG-1"), (1, 1))
        self.assertTrue(api.lock("ENG-1")["locked"])

    def test_an_issue_lock_is_exclusive_and_can_wait(self):
        self.assertTrue(api.lock("ENG-1")["locked"])
        busy = api.lock("ENG-1")
        self.assertFalse(busy["locked"])
        self.assertIn("token=", busy["holder"])
        clock = [0.0]
        out = api.lock("verify", wait=0)
        self.assertTrue(out["locked"])
        waited = api.lock("verify", wait=30, now=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        self.assertFalse(waited["locked"])
        self.assertGreaterEqual(clock[0], 30)
        api.unlock("verify", out["token"])
        self.assertTrue(api.lock("verify")["locked"])
        with self.assertRaises(SystemExit):
            api.lock("../etc")

    def test_capacity_counts_open_prs_and_running_issues(self):
        api.lock("ENG-5")  # a run on ENG-5, no PR yet
        out = api.sweep([API], gh=FakeGh({API: [pr(1, "ENG-1"), pr(2, "ENG-2", draft=True)]}))
        self.assertEqual(out["capacity"], 0)  # ENG-1, ENG-2 and ENG-5
        self.assertTrue(out["slot_taken"])
        self.assertEqual(out["running"], ["ENG-5"])
        self.assertEqual(out["next"]["issue"], "ENG-1")  # still finish what's there

    def test_an_issue_a_run_works_on_is_never_offered_to_another(self):
        api.lock("ENG-1")
        out = api.sweep([API], gh=FakeGh({API: [pr(1, "ENG-1"), pr(2, "ENG-2")]}))
        self.assertTrue(out["inflight"][1]["claimed"] or out["inflight"][0]["claimed"])
        self.assertEqual(out["next"]["issue"], "ENG-2")
        self.assertEqual(api.count([API], gh=FakeGh({API: [pr(1, "ENG-1")]})), 0)

    def test_deferred_work_starts_when_there_is_capacity(self):
        api.wait("ENG-9", "slot", "asked mid-flight", [API], gh=FakeGh({}))
        out = api.sweep([API], gh=FakeGh({API: [pr(1, "ENG-1")]}))
        self.assertEqual(out["capacity"], 2)
        self.assertEqual(out["deferred_next"]["issue"], "ENG-9")

    def test_overlap_names_other_issues_touching_the_same_files(self):
        def with_files(n, issue, files):
            r = pr(n, issue)
            r["files"] = [{"path": f} for f in files]
            return r
        gh = FakeGh({API: [with_files(1, "ENG-1", ["app/services/income.py", "README.md"]),
                           with_files(2, "ENG-2", ["app/routes/x.py"]),
                           with_files(3, "ENG-3", ["app/services/income.py"])]})
        out = api.overlap("ENG-3", ["app/services/income.py", "docs/a.md"], [API], gh=gh)
        self.assertEqual(out, {"conflicts": [{"issue": "ENG-1", "prs": ["https://x/1"], "files": ["app/services/income.py"]}]})
        self.assertEqual(api.overlap("ENG-9", ["nothing.py"], [API], gh=gh), {"conflicts": []})

    def test_deferred_work_waits_for_what_it_overlaps(self):
        api.wait("ENG-9", "slot", "same files as ENG-1", [API], gh=FakeGh({}), after=["ENG-1"])
        busy = api.sweep([API], gh=FakeGh({API: [pr(1, "ENG-1")]}))
        self.assertEqual(busy["capacity"], 2)
        self.assertIsNone(busy["deferred_next"])  # capacity, but ENG-1 is still open
        self.assertEqual(api.sweep([API], gh=FakeGh({}))["deferred_next"]["issue"], "ENG-9")


class PluginCheckTests(unittest.TestCase):
    """A run started on an older Autopilot must notice an update and hand over."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name, "plugin")
        (self.root / ".claude-plugin").mkdir(parents=True)
        self.installed = pathlib.Path(self.tmp.name, "installed_plugins.json")

    def tearDown(self):
        self.tmp.cleanup()

    def setup(self, running, installed):
        (self.root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": running}))
        entries = [{"scope": "user", "version": installed}] if installed else []
        self.installed.write_text(json.dumps({"plugins": {"autopilot@autopilot": entries}}))
        return api.plugin_check(str(self.root), str(self.installed))

    def test_a_newer_install_makes_the_run_stale(self):
        self.assertEqual(self.setup("0.9.0", "0.9.1"), {"running": "0.9.0", "installed": "0.9.1", "stale": True})

    def test_versions_compare_as_numbers_not_text(self):
        self.assertTrue(self.setup("0.9.0", "0.10.0")["stale"])

    def test_the_same_or_an_older_install_is_not_stale(self):
        self.assertFalse(self.setup("0.9.1", "0.9.1")["stale"])
        self.assertFalse(self.setup("0.9.1", "0.9.0")["stale"])

    def test_only_the_user_scope_install_counts(self):
        (self.root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "0.9.0"}))
        self.installed.write_text(json.dumps({"plugins": {"autopilot@autopilot": [
            {"scope": "project", "version": "0.9.5"}]}}))
        self.assertFalse(api.plugin_check(str(self.root), str(self.installed))["stale"])

    def test_unknown_install_state_never_stops_a_run(self):
        self.assertFalse(self.setup("0.9.0", None)["stale"])
        self.installed.unlink()
        out = api.plugin_check(str(self.root), str(self.installed))
        self.assertFalse(out["stale"])
        self.assertIn("error", out)

    def test_the_cli_prints_it(self):
        self.setup("0.9.0", "0.9.2")
        os.environ["AUTOPILOT_INSTALLED_PLUGINS"] = str(self.installed)
        try:
            out = api.run(["plugin-check"])
        finally:
            del os.environ["AUTOPILOT_INSTALLED_PLUGINS"]
        self.assertEqual(out["installed"], "0.9.2")
