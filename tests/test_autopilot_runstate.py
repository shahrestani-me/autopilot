"""Unit tests for bin/autopilot-runstate: carrying on after a Claude usage limit. Stdlib only."""

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def out(*objs):
    return {"stdout": "\n".join(json.dumps(o) for o in objs)}


class RunstateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["AUTOPILOT_STATE_DIR"] = self.tmp.name
        self.rs = _load("autopilot-runstate")
        self.ai = _load("autopilot-inflight")
        self.posts = []

    def tearDown(self):
        del os.environ["AUTOPILOT_STATE_DIR"]
        self.tmp.cleanup()

    def hold(self, sid="s1", issue="ENG-1"):
        """A run takes a slot and an issue lock the way the orchestrator does."""
        slot = self.ai.slot("take")
        lock = self.ai.lock(issue)
        self.rs.track(sid, out(slot))
        self.rs.track(sid, out(lock))
        return slot, lock

    def post(self, issue, content):
        self.posts.append((issue, content))

    def test_tracks_slot_and_issue_lock_from_helper_output(self):
        slot, lock = self.hold()
        self.assertEqual(self.rs.read("s1"), {"slot": slot["slot"], "slot_token": slot["token"],
                                              "issue": "ENG-1", "issue_token": lock["token"]})

    def test_the_agent_named_at_slot_take_is_kept_until_release(self):
        slot = self.ai.slot("take", agent="autopilot-agent-2")
        self.assertEqual(slot["agent"], "autopilot-agent-2")
        self.rs.track("s1", out(slot))
        self.assertEqual(self.rs.read("s1")["agent"], "autopilot-agent-2")
        self.rs.track("s1", out({"released": slot["slot"]}))
        self.assertEqual(self.rs.read("s1"), {})
        self.rs.track("s1", out({"slot": 1, "token": "t", "agent": "bad name`"}))
        self.assertNotIn("agent", self.rs.read("s1"))

    def test_run_by_names_the_agent(self):
        self.assertEqual(self.rs.run_by("autopilot-agent"), "Run by Agent 1 (`autopilot-agent`)")
        self.assertEqual(self.rs.run_by("autopilot-agent-2"), "Run by Agent 2 (`autopilot-agent-2`)")
        self.assertEqual(self.rs.run_by("nightly"), "Run by `nightly`")
        self.assertEqual(self.rs.run_by(None), "Manual run (no scheduled agent)")
        self.assertEqual(self.rs.run_by("x`; rm"), "Manual run (no scheduled agent)")
        self.assertEqual(self.rs.run_by(7), "Manual run (no scheduled agent)")  # a hand-edited record

    def test_the_paused_note_names_the_agent(self):
        slot = self.ai.slot("take", agent="autopilot-agent-3")
        self.rs.track("s1", out(slot))
        self.rs.track("s1", out(self.ai.lock("ENG-1")))
        self.rs.on_failure("s1", {"error": "rate_limit", "last_assistant_message": "Usage limit reached"},
                           inflight=self.ai, post=self.post)
        self.assertIn("Run by Agent 3 (`autopilot-agent-3`).", self.posts[0][1]["body"])

    def test_a_usage_limit_frees_the_issue_and_does_not_count_as_an_attempt(self):
        self.hold()
        done = self.rs.on_failure("s1", {"error": "rate_limit", "last_assistant_message": "Usage limit reached"},
                                  inflight=self.ai, post=self.post)
        self.assertEqual(done["unlocked"], "ENG-1")
        self.assertEqual(done["released"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "locks", "ENG-1.lock")))
        self.assertEqual(self.ai.slot("busy"), 0)
        self.assertEqual(self.ai.interrupted("ENG-1")["count"], 1)
        self.assertEqual(self.rs.read("s1"), {})
        self.assertEqual(self.posts[0][0], "ENG-1")
        self.assertIn("Usage limit reached", self.posts[0][1]["body"])
        # The next run takes it straight back, and its claim is not an automatic retry.
        self.assertTrue(self.ai.lock("ENG-1")["locked"])
        self.assertNotIn("auto_retries", self.ai.claim("ENG-1"))

    def test_only_the_claim_right_after_an_interruption_is_free(self):
        self.hold()
        self.rs.on_failure("s1", {"error": "rate_limit"}, inflight=self.ai, post=self.post)
        self.ai.claim("ENG-1")
        rec = self.ai.claim("ENG-1")  # a later run, no new interruption since: counts again
        self.assertEqual(rec["auto_retries"], 1)

    def test_a_block_keeps_which_interruptions_were_already_used(self):
        self.hold()
        self.rs.on_failure("s1", {"error": "rate_limit"}, inflight=self.ai, post=self.post)
        self.ai.claim("ENG-1")
        self.ai.wait("ENG-1", "checkable", "red check", repos=[], gh=lambda args: "[]")
        self.assertEqual(self.ai.claim("ENG-1")["auto_retries"], 1)

    def test_an_unknown_error_that_names_a_limit_counts_as_one(self):
        self.hold()
        done = self.rs.on_failure("s1", {"error": "unknown", "last_assistant_message": "You've hit your limit · resets 3am"},
                                  inflight=self.ai, post=self.post)
        self.assertEqual(done["unlocked"], "ENG-1")

    def test_an_unknown_error_without_a_limit_is_left_alone(self):
        self.hold()
        self.assertIn("skipped", self.rs.on_failure("s1", {"error": "unknown", "last_assistant_message": "boom"},
                                                    inflight=self.ai, post=self.post))

    def test_other_errors_are_left_to_the_attempt_cap(self):
        self.hold()
        done = self.rs.on_failure("s1", {"error": "invalid_request"}, inflight=self.ai, post=self.post)
        self.assertEqual(done, {"skipped": "invalid_request"})
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "locks", "ENG-1.lock")))
        self.assertEqual(self.ai.interrupted("ENG-1")["count"], 0)
        self.assertEqual(self.posts, [])

    def test_a_session_holding_nothing_is_untouched(self):
        done = self.rs.on_failure("other", {"error": "rate_limit"}, inflight=self.ai, post=self.post)
        self.assertEqual(done, {"skipped": "rate_limit"})
        self.assertEqual(self.posts, [])

    def test_only_a_usage_limit_switches_to_api_mode(self):
        self.assertTrue(self.rs.is_limit({"error": "rate_limit"}))
        self.assertTrue(self.rs.is_limit({"error": "billing_error"}))
        self.assertTrue(self.rs.is_limit({"error": "unknown", "last_assistant_message": "You've hit your limit · resets 3pm"}))
        for weekly in ("You've reached your weekly limit · resets Mon 9:00 AM",
                       "Weekly limit reached ∙ resets Oct 28, 9am",
                       "Claude usage limit reached. Your limit will reset at 5pm",
                       "You've hit your Opus weekly limit"):
            with self.subTest(weekly=weekly):
                self.assertTrue(self.rs.is_limit({"error": "unknown", "last_assistant_message": weekly}))
        self.assertFalse(self.rs.is_limit({"error": "overloaded"}))
        self.assertFalse(self.rs.is_limit({"error": "server_error"}))
        self.assertFalse(self.rs.is_limit({"error": "unknown", "last_assistant_message": "connection reset by peer"}))

    def test_switch_to_api_mode_records_the_limit_then_starts(self):
        calls = []
        self.rs.switch_to_api_mode(run=lambda argv, **kw: calls.append(argv[1:]))
        self.assertEqual(calls, [["limited"], ["start", "--reason", "Claude usage limit"]])

    def test_a_fresh_limit_survives_another_sessions_command(self):
        path = os.path.join(self.tmp.name, "claude-limited")
        with open(path, "w") as f:
            f.write("2026-10-25T10:00:00Z\n")
        import time
        self.assertFalse(self.rs.clear_limit(now=time.time() + 60))
        self.assertTrue(os.path.exists(path))
        self.assertTrue(self.rs.clear_limit(now=time.time() + self.rs.CLEAR_AFTER_S + 1))
        self.assertFalse(os.path.exists(path))

    def test_an_api_mode_run_is_recognised(self):
        self.assertTrue(self.rs.in_api_mode_run({"AUTOPILOT_FALLBACK": "1"}))
        self.assertFalse(self.rs.in_api_mode_run({}))

    def test_a_normal_release_forgets_the_run(self):
        slot, lock = self.hold()
        self.rs.track("s1", out(self.ai.unlock("ENG-1", lock["token"])))
        self.rs.track("s1", out(self.ai.slot("release", slot["slot"], token=slot["token"])))
        self.assertEqual(self.rs.read("s1"), {})

    def test_a_lock_already_taken_over_is_not_freed(self):
        slot, lock = self.hold()
        os.remove(os.path.join(self.tmp.name, "locks", "ENG-1.lock"))
        other = self.ai.lock("ENG-1")  # another run took it after ours looked dead
        done = self.rs.on_failure("s1", {"error": "rate_limit"}, inflight=self.ai, post=self.post)
        self.assertIn("unlock_error", done)
        self.assertEqual(self.ai.unlock("ENG-1", other["token"]), {"unlocked": "ENG-1"})

    def test_old_leftover_records_are_pruned(self):
        self.rs.write("dead", {"issue": "ENG-9", "issue_token": "x"})
        old = os.path.getmtime(self.rs._path("dead")) - 3 * 86400
        os.utime(self.rs._path("dead"), (old, old))
        self.hold("fresh")
        self.assertEqual(self.rs.read("dead"), {})
        self.assertNotEqual(self.rs.read("fresh"), {})

    def test_clear_forgets_interruptions(self):
        self.ai.record_interruption("ENG-1", "rate_limit")
        self.ai.clear("ENG-1")
        self.assertEqual(self.ai.interrupted("ENG-1")["count"], 0)


if __name__ == "__main__":
    unittest.main()
