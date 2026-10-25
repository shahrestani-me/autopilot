"""Unit tests for bin/autopilot-fallback (API mode). Stdlib only; no network, no Keychain, no claude."""

import datetime
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW = datetime.datetime(2026, 10, 25, 12, 0, tzinfo=datetime.timezone.utc)


def _load():
    loader = importlib.machinery.SourceFileLoader("autopilot_fallback", str(ROOT / "bin" / "autopilot-fallback"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FakeOR:
    def __init__(self, usage=(1.0,)):
        self.usage_seq = list(usage)

    def key(self):
        return "sk-or-test"

    def usage(self):
        v = self.usage_seq.pop(0) if len(self.usage_seq) > 1 else self.usage_seq[0]
        return {"usage_usd": v}


class FakeProc:
    def __init__(self, polls):
        self._polls = list(polls)
        self.stdin = mock.Mock()
        self.returncode = None
        self.terminated = False

    def poll(self):
        r = self._polls.pop(0) if self._polls else 0
        if r is not None:
            self.returncode = r
        return r

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["AUTOPILOT_STATE_DIR"] = self.tmp.name
        os.environ["AUTOPILOT_OPENROUTER_CONFIG"] = os.path.join(self.tmp.name, "openrouter.json")
        os.environ["AUTOPILOT_CLAUDE_BIN"] = "/usr/bin/true"
        self.fb = _load()

    def tearDown(self):
        for k in ("AUTOPILOT_STATE_DIR", "AUTOPILOT_OPENROUTER_CONFIG", "AUTOPILOT_CLAUDE_BIN"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def touch(self, name, text=""):
        with open(os.path.join(self.tmp.name, name), "w") as f:
            f.write(text)

    def test_defaults_are_the_three_role_models(self):
        cfg = self.fb.settings()
        self.assertEqual((cfg["orchestrator"], cfg["reviewer"], cfg["coder"]),
                         ("openai/gpt-5.6-terra", "openai/gpt-5.6-sol", "deepseek/deepseek-v4-pro"))

    def test_settings_refuse_claude_unknown_keys_and_bad_json(self):
        for body, msg in (({"reviewer": "anthropic/claude-opus-5.5"}, "non-Claude"), ({"model": "x/y"}, "unknown"), ("{", "JSON")):
            with self.subTest(body=body):
                self.touch("openrouter.json", body if isinstance(body, str) else json.dumps(body))
                with self.assertRaisesRegex(self.fb.LinearError, msg):
                    self.fb.settings()

    def test_active_for_api_mode_or_a_fresh_limit_never_when_paused(self):
        self.assertIsNone(self.fb.active(NOW))
        self.touch("claude-limited", "2026-10-25T10:00:00Z\n")
        self.assertEqual(self.fb.active(NOW), "Claude usage limit")
        # A weekly limit lasts days: the flag holds until Claude works again (the hook clears it).
        self.assertEqual(self.fb.active(NOW + datetime.timedelta(days=3)), "Claude usage limit")
        self.assertIsNone(self.fb.active(NOW + datetime.timedelta(days=9)))  # the backstop, and removed
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "claude-limited")))
        self.touch("api-mode", "2026-10-25T10:00:00Z\n")
        self.assertEqual(self.fb.active(NOW), "api mode on")
        self.touch("paused")
        self.assertIsNone(self.fb.active(NOW))

    def test_daily_spend_adds_up_per_day(self):
        self.fb.add_spend(1.25, NOW)
        self.fb.add_spend(0.5, NOW)
        self.fb.add_spend(3.0, NOW + datetime.timedelta(days=1))
        self.assertEqual(self.fb.spent_today(NOW), 1.75)
        self.assertEqual(self.fb.spent_today(NOW + datetime.timedelta(days=1)), 3.0)

    def test_child_env_gives_each_role_its_model(self):
        cfg = self.fb.settings()
        env = self.fb.child_env({"PATH": "/bin", "CLAUDECODE": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "x/y",
                                 "CLAUDE_CODE_OAUTH_TOKEN": "oauth"}, "sk-or-test", cfg, 5)
        self.assertEqual(env["ANTHROPIC_MODEL"], "openai/gpt-5.6-terra")
        for tier in ("OPUS", "SONNET", "FABLE"):
            self.assertEqual(env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"], "openai/gpt-5.6-sol")
        self.assertEqual(env["AUTOPILOT_CODER_MODEL"], "deepseek/deepseek-v4-pro")
        self.assertEqual(env["AUTOPILOT_FALLBACK"], "1")
        self.assertEqual(env["AUTOPILOT_FALLBACK_USD"], "5.00")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")
        for gone in ("CLAUDECODE", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_OAUTH_TOKEN"):
            self.assertNotIn(gone, env)

    def test_a_run_over_budget_is_stopped(self):
        proc = FakeProc([None] * 10)
        with tempfile.TemporaryFile("w+") as log:
            r = self.fb.run_once(FakeOR((1.0, 7.0)), self.fb.settings(), "api mode on", 5.0, log,
                                 popen=lambda *a, **kw: proc, sleep=lambda s: None)
        self.assertEqual(r["stopped"], "budget")
        self.assertTrue(proc.terminated)
        self.assertEqual(r["cost_usd"], 6.0)
        sent = "".join(c.args[0] for c in proc.stdin.write.call_args_list)
        self.assertIn("Agent: autopilot-fallback", sent)
        self.assertIn("$5.00", sent)
        self.assertIn("heredoc", sent)
        self.assertIn("No $(...)", sent)
        self.assertIn("Every review runs in a subagent on openai/gpt-5.6-sol", sent)
        self.assertIn('"Coder model:" line', sent)
        self.assertIn(os.path.join(self.tmp.name, "tmp"), sent)

    def test_a_run_that_lingers_after_its_result_is_stopped(self):
        proc = FakeProc([None] * 50)
        clock = iter(range(0, 10_000, 30))
        log_path = os.path.join(self.tmp.name, "run.log")
        with open(log_path, "a+") as log:
            def popen(*a, **kw):
                kw["stdout"].write('{"type":"result","subtype":"success","result":"done"}\n')
                kw["stdout"].flush()
                return proc
            r = self.fb.run_once(FakeOR((1.0,)), self.fb.settings(), "api mode on", 5.0, log,
                                 popen=popen, sleep=lambda s: None, now=lambda: next(clock))
        self.assertEqual(r["stopped"], "finished")
        self.assertTrue(proc.terminated)
        self.assertLess(r["seconds"], 300)

    def test_a_run_waiting_for_its_background_task_is_not_stopped(self):
        """ENG-219: the run started the coder in the background and wrote a result while waiting."""
        state = {"open": set(), "idle_result": False}
        start = '{"type":"system","subtype":"task_started","task_id":"b1"}'
        result = '{"type":"result","subtype":"success"}'
        done = '{"type":"system","subtype":"task_notification","task_id":"b1","status":"completed"}'
        self.fb.stream_events(start + "\n" + result + "\n", state)
        self.assertFalse(state["idle_result"])  # waiting for b1: keep it running
        self.fb.stream_events(done + "\n", state)
        self.assertFalse(state["idle_result"])  # the task ended: the run goes on
        self.fb.stream_events(result + "\n", state)
        self.assertTrue(state["idle_result"])   # a result with nothing open: now it may linger

    def test_leftover_lock_and_slot_are_released_after_a_run(self):
        ai = self.fb._load("autopilot-inflight")
        slot = ai.slot("take")
        lock = ai.lock("ENG-219")
        os.makedirs(os.path.join(self.tmp.name, "runs"), exist_ok=True)
        self.touch("runs/s-1.json", json.dumps({"slot": slot["slot"], "slot_token": slot["token"],
                                                "issue": "ENG-219", "issue_token": lock["token"]}))
        self.assertEqual(self.fb.release_leftovers({"s-1", "s-unknown", "../x"}, inflight=ai), ["ENG-219", f"slot {slot['slot']}"])
        self.assertTrue(ai.lock("ENG-219")["locked"])  # free again
        self.assertEqual(ai.slot("busy"), 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "runs", "s-1.json")))

    def test_helpers_run_by_bare_name_from_the_installed_plugin(self):
        installed = os.path.join(self.tmp.name, "installed.json")
        self.touch("installed.json", json.dumps({"plugins": {"autopilot@autopilot": [{"installPath": "/p/0.15.9"}]}}))
        self.assertEqual(self.fb.skills_root(installed), "/p/0.15.9")
        env = self.fb.child_env({"PATH": "/usr/bin"}, "k", self.fb.settings(), 5, root="/p/0.15.9")
        self.assertEqual(env["PATH"], "/p/0.15.9/bin:/usr/bin")
        rules = self.fb.allowed_tools(self.fb.settings(), roots=["/p/0.15.9"])
        self.assertIn("Bash(autopilot-inflight:*)", rules)
        self.assertIn("Bash(autopilot-github:*)", rules)
        text = self.fb.PROMPT.format(agent="a", reason="r", budget=1, state="/s", **self.fb.settings())
        self.assertIn("AG=autopilot-github", text)
        self.assertIn("never write a path", text)

    def test_supervisor_works_the_queue_then_stops_at_the_daily_cap(self):
        self.touch("api-mode", "2026-10-25T10:00:00Z\n")
        self.touch("openrouter.json", json.dumps({"usd_per_day": 8, "usd_per_ticket": 5}))
        budgets = []

        def run(orouter, cfg, reason, budget, log):
            budgets.append(budget)
            return {"cost_usd": 4.0, "seconds": 600, "exit": 0, "stopped": None}

        runs = self.fb.supervise(orouter=FakeOR(), run=run, count=lambda: 3, sleep=lambda s: None)
        self.assertEqual(len(runs), 2)
        self.assertEqual(budgets, [5, 4])  # the second run only gets what's left of the day
        self.assertGreaterEqual(self.fb.spent_today(), 8)

    def test_supervisor_stops_when_the_queue_is_empty_or_api_mode_ends(self):
        self.touch("api-mode", "2026-10-25T10:00:00Z\n")
        self.assertEqual(self.fb.supervise(orouter=FakeOR(), run=None, count=lambda: 0), [])

        def run(orouter, cfg, reason, budget, log):
            os.remove(os.path.join(self.tmp.name, "api-mode"))  # the owner turns it off mid-run
            return {"cost_usd": 0.5, "seconds": 600, "exit": 0, "stopped": None}

        self.assertEqual(len(self.fb.supervise(orouter=FakeOR(), run=run, count=lambda: 5)), 1)

    def test_supervisor_stops_when_runs_leave_the_queue_as_it_was(self):
        self.touch("api-mode", "2026-10-25T10:00:00Z\n")
        run = lambda *a: {"cost_usd": 0.05, "seconds": 600, "exit": 0, "stopped": None}
        runs = self.fb.supervise(orouter=FakeOR(), run=run, count=lambda: 1, sleep=lambda s: None)
        self.assertEqual(len(runs), self.fb.NO_PROGRESS)  # a claude wait it can't take: no paid loop

    def test_supervisor_gives_up_on_runs_that_end_at_once(self):
        """Out of credit, setup broken: quick runs that leave the queue as it was."""
        self.touch("api-mode", "2026-10-25T10:00:00Z\n")
        sleeps = []
        run = lambda *a: {"cost_usd": 0.01, "seconds": 5, "exit": 1, "stopped": None}
        runs = self.fb.supervise(orouter=FakeOR(), run=run, count=lambda: 3, sleep=sleeps.append)
        self.assertEqual(len(runs), self.fb.NO_PROGRESS)
        self.assertEqual(sleeps, [self.fb.IDLE_S] * (self.fb.NO_PROGRESS - 1))  # a pause between them
        self.assertIsNotNone(self.fb.gave_up(count=lambda: 3))

    def test_a_quick_run_that_moved_the_queue_does_not_pause_the_next(self):
        self.touch("api-mode", "x\n")
        left = iter([2, 1, 1, 0, 0])  # before run 1, after run 1 (shrank), before run 2, after run 2, end
        sleeps = []
        run = lambda *a: {"cost_usd": 0.2, "seconds": 95, "exit": 0, "stopped": None}
        runs = self.fb.supervise(orouter=FakeOR(), run=run, count=lambda: next(left), sleep=sleeps.append)
        self.assertEqual(len(runs), 2)
        self.assertEqual(sleeps, [])  # no 5-minute pause between two quick runs that made progress

    def test_supervisor_errors_are_logged_and_free_the_lock(self):
        self.touch("api-mode", "2026-10-25T10:00:00Z\n")
        log = os.path.join(self.tmp.name, "fallback.log")

        def run(*a):
            raise self.fb.LinearError("OpenRouter GET /v1/key failed (HTTP 0)")

        self.fb.supervise(orouter=FakeOR(), run=run, count=lambda: 1, log_path=log)
        self.assertIn("HTTP 0", pathlib.Path(log).read_text())
        self.assertIsNone(self.fb.running())

    def test_queue_count_sees_the_queue_as_an_api_mode_run(self):
        seen = []

        def fake_run(argv, **kw):
            seen.append(kw["env"].get("AUTOPILOT_FALLBACK"))
            return mock.Mock(returncode=0, stdout="2\n")

        with mock.patch.object(self.fb.subprocess, "run", fake_run):
            self.assertEqual(self.fb.queue_count(), 4)
        self.assertEqual(seen, ["1", "1"])

    def test_settings_numbers_must_be_positive_numbers(self):
        for bad in ({"usd_per_day": "20"}, {"usd_per_ticket": 0}, {"timeout_min": True}, {"permission_mode": "plan"}):
            with self.subTest(bad=bad):
                self.touch("openrouter.json", json.dumps(bad))
                with self.assertRaises(self.fb.LinearError):
                    self.fb.settings()

    def test_after_giving_up_only_a_changed_queue_or_the_owner_restarts_it(self):
        self.touch("claude-limited", self.fb._now().strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
        run = lambda *a: {"cost_usd": 0.05, "seconds": 600, "exit": 0, "stopped": None}
        self.fb.supervise(orouter=FakeOR(), run=run, count=lambda: 1, sleep=lambda s: None)
        popen = mock.Mock()
        with mock.patch.object(self.fb.apc, "OpenRouter", return_value=FakeOR()):
            self.assertIn("gave up", self.fb.start("tick", popen=popen, count=lambda: 1)["why"])
            self.assertIn("gave up", self.fb.start("Claude usage limit", popen=popen, count=lambda: 1)["why"])
            popen.assert_not_called()
            self.assertTrue(self.fb.start("tick", popen=popen, count=lambda: 2)["started"])  # new work arrived
            self.fb.give_up("test", 1)
            self.touch("api-mode", "x\n")
            self.assertIn("gave up", self.fb.start("api mode on", popen=popen, count=lambda: 1)["why"])  # a reason isn't the owner
            self.assertTrue(self.fb.start("api mode on", popen=popen, count=lambda: 1, owner=True)["started"])  # the owner

    def test_start_refuses_when_off_spent_or_already_running(self):
        popen = mock.Mock()
        self.assertFalse(self.fb.start("x", popen=popen)["started"])
        self.touch("api-mode", "2026-10-25T10:00:00Z\n")
        with mock.patch.object(self.fb.apc, "OpenRouter", return_value=FakeOR()):
            self.assertTrue(self.fb.start("x", popen=popen)["started"])
            popen.assert_called_once()
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            with mock.patch.object(self.fb, "running", return_value=123):
                self.assertIn("already running", self.fb.start("x", popen=popen)["why"])
            self.fb.add_spend(25)
            self.assertIn("budget", self.fb.start("x", popen=popen)["why"])

    def test_the_lock_admits_one_supervisor(self):
        import fcntl
        self.touch("api-mode", "2026-10-25T10:00:00Z\n")
        fd = os.open(os.path.join(self.tmp.name, "fallback.lock"), os.O_RDWR | os.O_CREAT)
        os.write(fd, b"4242")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertEqual(self.fb.running(), 4242)
            self.assertEqual(self.fb.supervise(orouter=FakeOR(), run=None, count=lambda: 1), [])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertIsNone(self.fb.running())

    def test_tick_starts_a_supervisor_only_when_needed(self):
        with mock.patch.object(self.fb, "start", return_value={"started": True}) as start:
            self.assertIsNone(self.fb.tick())  # API mode off: nothing, not even a log line
            self.touch("claude-limited", self.fb._now().strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
            with mock.patch.object(self.fb, "running", return_value=99):
                self.assertIsNone(self.fb.tick())  # one already runs
            self.assertEqual(self.fb.tick(), {"started": True})
            start.assert_called_once_with("tick")
        self.assertIn("started", pathlib.Path(self.tmp.name, "fallback-tick.log").read_text())

    def test_the_tick_plist_runs_tick_with_the_owners_path(self):
        xml = self.fb._plist("/x/bin/autopilot-fallback", "/opt/homebrew/bin:/usr/bin")
        self.assertIn("<string>tick</string>", xml)
        self.assertIn("<key>AbandonProcessGroup</key><true/>", xml)
        self.assertIn("/opt/homebrew/bin:/usr/bin", xml)
        self.assertIn(f"<integer>{self.fb.TICK_S}</integer>", xml)
        import plistlib
        self.assertEqual(plistlib.loads(xml.encode())["Label"], self.fb.TICK_LABEL)

    def test_config_command_shows_and_sets(self):
        out = self.fb.run(["config"])
        self.assertEqual(out["settings"]["usd_per_day"], 20.0)
        self.assertEqual(self.fb.run(["config", "set", "usd_per_day", "35"])["settings"]["usd_per_day"], 35)
        with self.assertRaises(self.fb.LinearError):
            self.fb.run(["config", "set", "usd_per_day"])

    def test_runs_get_exact_rules_for_the_helpers_and_the_owners_extras(self):
        self.touch("openrouter.json", json.dumps({"allowed_tools": ["Bash(npm test:*)"]}))
        installed = os.path.join(self.tmp.name, "installed.json")
        self.touch("installed.json", json.dumps({"plugins": {"autopilot@autopilot": [{"installPath": "/p/cache/0.15.1"}]}}))
        rules = self.fb.allowed_tools(self.fb.settings(), roots=self.fb.plugin_roots(installed))
        self.assertIn("Bash(/p/cache/0.15.1/bin/autopilot-inflight:*)", rules)
        self.assertIn("Bash(/p/cache/0.15.1/bin/autopilot-linear:*)", rules)
        self.assertIn("Bash(npm test:*)", rules)
        self.assertNotIn("Bash", rules)  # never every command
        self.assertFalse(any("*/" in r for r in rules))  # a wildcard in a path never matches

    def test_allowed_tools_must_be_a_short_list_of_strings(self):
        for bad in ({"allowed_tools": "Bash"}, {"allowed_tools": [1]}, {"allowed_tools": ["x"] * 51}):
            with self.subTest(bad=bad):
                self.touch("openrouter.json", json.dumps(bad))
                with self.assertRaises(self.fb.LinearError):
                    self.fb.settings()

    def test_mode_and_limit_commands(self):
        with mock.patch.object(self.fb, "start", return_value={"started": False, "why": "test"}):
            self.assertTrue(self.fb.run(["mode", "on"])["api_mode"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "api-mode")))
        self.assertTrue(self.fb.run(["mode", "off"])["removed"])
        self.fb.run(["limited"])
        self.assertEqual(self.fb.active(), "Claude usage limit")
        self.assertTrue(self.fb.run(["clear-limit"])["cleared"])
        self.assertIsNone(self.fb.active())


if __name__ == "__main__":
    unittest.main()
