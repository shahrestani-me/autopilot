"""Unit tests for bin/autopilot-coder. Stdlib only; no network, no Keychain, no claude."""

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
# The module reads its config and ledger paths at load: point them at a scratch folder, never the
# owner's real ~/.config/autopilot/openrouter.json or spend ledger.
_SCRATCH = tempfile.mkdtemp()
os.environ.setdefault("AUTOPILOT_STATE_DIR", _SCRATCH)
os.environ.setdefault("AUTOPILOT_OPENROUTER_CONFIG", os.path.join(_SCRATCH, "openrouter.json"))
_loader = importlib.machinery.SourceFileLoader("autopilot_coder", str(ROOT / "bin" / "autopilot-coder"))
_spec = importlib.util.spec_from_loader("autopilot_coder", _loader)
apc = importlib.util.module_from_spec(_spec)
_loader.exec_module(apc)

MODELS = [
    {"id": "deepseek/deepseek-v4.1-flash", "name": "DeepSeek: DeepSeek V4.1 Flash"},
    {"id": "deepseek/deepseek-v4.1-flash-1017", "name": "DeepSeek: DeepSeek V4.1 Flash 1017"},
    {"id": "deepseek/deepseek-v4.1-flash:batch", "name": "DeepSeek: DeepSeek V4.1 Flash (batch)"},
    {"id": "~deepseek/deepseek-flash-latest", "name": "DeepSeek Flash Latest"},
    {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek: DeepSeek V4 Flash"},
    {"id": "openai/gpt-5", "name": "OpenAI: GPT-5"},
    {"id": "openai/gpt-5-mini", "name": "OpenAI: GPT-5 Mini"},
    {"id": "anthropic/claude-sonnet-5.5", "name": "Anthropic: Claude Sonnet 5.5"},
]


class FakeKeychain:
    def __init__(self, key="sk-or-test"):
        self.key = key

    def get(self, account):
        return self.key if account == apc.ACCOUNT else None


class FakeAPI:
    """OpenRouter: /v1/key reports `usage`, which a run's ticks raise; /v1/models lists MODELS."""

    def __init__(self, usage=(1.0,), status=200):
        self.usage = list(usage)  # a value per /v1/key read (the last repeats); None = network error
        self.status = status
        self.calls = []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, headers.get("Authorization")))
        if url.endswith("/v1/models"):
            return 200, {"data": MODELS}
        value = self.usage.pop(0) if len(self.usage) > 1 else self.usage[0]
        if value is None:
            return 0, {"networkError": "down"}
        return self.status, {"data": {"usage": value, "limit_remaining": 9.0}}


class FakeProc:
    def __init__(self, polls, stdout='{"result": "done: 2 files", "num_turns": 7}', code=0):
        self._polls = list(polls)  # None while running, then the exit code
        self.stdin = mock.Mock()
        self.stdout_text = stdout
        self.stdout_file = None
        self.returncode = None
        self._code = code
        self.terminated = False

    def poll(self):
        r = self._polls.pop(0) if self._polls else self._code
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

    def started(self, stdout):
        stdout.write(self.stdout_text)  # what claude -p prints, into the file it was given
        return self


class ResolveModelTests(unittest.TestCase):
    def test_words_pick_the_base_id_over_dated_snapshots(self):
        self.assertEqual(apc.resolve_model("DeepSeek flash 4.1", MODELS)["id"], "deepseek/deepseek-v4.1-flash")

    def test_other_variants_are_ambiguous_not_guessed(self):
        with self.assertRaisesRegex(apc.CoderError, "several.*gpt-5, openai/gpt-5-mini"):
            apc.resolve_model("gpt-5", MODELS)
        self.assertEqual(apc.resolve_model("openai/gpt-5", MODELS)["id"], "openai/gpt-5")

    def test_exact_id_and_alias(self):
        self.assertEqual(apc.resolve_model("deepseek/deepseek-v4.1-flash:batch", MODELS)["id"], "deepseek/deepseek-v4.1-flash:batch")
        self.assertEqual(apc.resolve_model("DeepSeek", MODELS, {"deepseek": "deepseek/deepseek-v4-flash"})["id"], "deepseek/deepseek-v4-flash")

    def test_ambiguous_or_unknown_raises_with_candidates(self):
        with self.assertRaisesRegex(apc.CoderError, "several.*deepseek-v4-flash.*deepseek-v4.1-flash"):
            apc.resolve_model("deepseek flash", MODELS)
        with self.assertRaisesRegex(apc.CoderError, "no OpenRouter model"):
            apc.resolve_model("llama 9", MODELS)

    def test_claude_is_refused(self):
        for q in ("claude sonnet", "anthropic/claude-sonnet-5.5", "opus"):
            with self.subTest(q=q), self.assertRaisesRegex(apc.CoderError, "Claude model"):
                apc.resolve_model(q, MODELS)


class ChildTests(unittest.TestCase):
    def test_env_points_every_tier_at_openrouter_and_drops_the_claude_login(self):
        base = {"PATH": "/bin", "CLAUDECODE": "1", "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "ANTHROPIC_API_KEY": "sk-ant"}
        env = apc.child_env(base, "sk-or-test", "openai/gpt-5")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://openrouter.ai/api")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-or-test")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        for k in ("ANTHROPIC_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
            self.assertEqual(env[k], "openai/gpt-5")
        self.assertEqual(env["PATH"], "/bin")

    def test_argv_allows_only_file_tools_inside_the_worktree(self):
        argv = apc.child_argv("/usr/bin/claude", "openai/gpt-5", "/Users/me/wt/fix-x")
        self.assertNotIn("sk-or-test", " ".join(argv))
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        self.assertEqual(allowed, ["Read(//Users/me/wt/fix-x/**)", "Edit(//Users/me/wt/fix-x/**)", "Write(//Users/me/wt/fix-x/**)"])
        for tool in ("Bash", "WebFetch", "Agent"):
            self.assertIn(tool, argv[argv.index("--disallowedTools") + 1:])


class RunCoderTests(unittest.TestCase):
    def setUp(self):
        self.wt = tempfile.mkdtemp()
        os.environ["AUTOPILOT_CLAUDE_BIN"] = "/usr/bin/true"
        self.addCleanup(os.environ.pop, "AUTOPILOT_CLAUDE_BIN", None)

    def _run(self, api, proc, max_usd=1.0, model="openai/gpt-5", prompt="Add a test"):
        orouter = apc.OpenRouter(keychain=FakeKeychain(), request=api)
        seen = {}

        def popen(argv, **kw):
            seen.update(argv=argv, proc=proc, **kw)
            return proc.started(kw["stdout"])

        out = apc.run_coder(orouter, model, self.wt, prompt, max_usd, 60, popen=popen, sleep=lambda s: None)
        return out, seen

    @staticmethod
    def proc_stdin(seen):
        return "".join(c.args[0] for c in seen["proc"].stdin.write.call_args_list)

    def test_success_reports_cost_from_the_key_usage(self):
        out, seen = self._run(FakeAPI(usage=(1.0, 1.25)), FakeProc([None, 0]))
        self.assertTrue(out["ok"])
        self.assertEqual(out["cost_usd"], 0.25)
        self.assertEqual(out["result"], "done: 2 files")
        self.assertEqual(seen["cwd"], self.wt)
        self.assertEqual(seen["env"]["ANTHROPIC_AUTH_TOKEN"], "sk-or-test")
        sent = seen["argv"] and self.proc_stdin(seen)
        self.assertIn("Add a test", sent)
        self.assertIn(self.wt, sent)

    def test_over_budget_stops_the_model(self):
        proc = FakeProc([None, None, None, None])
        out, _ = self._run(FakeAPI(usage=(1.0, 3.0)), proc, max_usd=1.0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["stopped"], "budget")
        self.assertTrue(proc.terminated)
        self.assertEqual(out["cost_usd"], 2.0)

    def test_usage_read_failures_retry_then_stop(self):
        proc = FakeProc([None] * 30)
        out, _ = self._run(FakeAPI(usage=(1.0, None, 1.1, None, None, None)), proc)
        self.assertEqual(out["stopped"], "usage-unknown")
        self.assertTrue(proc.terminated)
        self.assertFalse(out["cost_measured"])

    def test_an_error_mid_run_still_stops_the_model(self):
        proc = FakeProc([None] * 10)
        orouter = apc.OpenRouter(keychain=FakeKeychain(), request=FakeAPI())
        boom = mock.Mock(side_effect=[None, KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            apc.run_coder(orouter, "openai/gpt-5", self.wt, "x", 1, 60, popen=lambda a, **kw: proc.started(kw["stdout"]), sleep=boom)
        self.assertTrue(proc.terminated)

    def test_error_result_is_not_ok(self):
        out, _ = self._run(FakeAPI(), FakeProc([0], stdout='{"is_error": true, "result": "API Error"}'))
        self.assertFalse(out["ok"])
        self.assertEqual(out["result"], "API Error")

    def test_refuses_claude_bad_id_missing_worktree_and_empty_prompt(self):
        for kwargs in ({"model": "anthropic/claude-sonnet-5.5"}, {"model": "gpt five"}, {"prompt": "  "}):
            with self.subTest(kwargs=kwargs), self.assertRaises(apc.CoderError):
                self._run(FakeAPI(), FakeProc([0]), **kwargs)
        orouter = apc.OpenRouter(keychain=FakeKeychain(), request=FakeAPI())
        with self.assertRaisesRegex(apc.CoderError, "no such worktree"):
            apc.run_coder(orouter, "openai/gpt-5", "/nonexistent/wt", "x", 1, 60)


class CliTests(unittest.TestCase):
    def test_check_without_key_is_setup_needed(self):
        orouter = apc.OpenRouter(keychain=FakeKeychain(key=None), request=FakeAPI())
        with self.assertRaises(apc.SetupError):
            apc.run(["check"], orouter=orouter)

    def test_check_with_a_rejected_key_is_setup_needed(self):
        orouter = apc.OpenRouter(keychain=FakeKeychain(), request=FakeAPI(status=401))
        with self.assertRaises(apc.SetupError):
            apc.run(["check"], orouter=orouter)

    def test_model_uses_aliases_and_the_public_list(self):
        api = FakeAPI()
        orouter = apc.OpenRouter(keychain=FakeKeychain(key=None), request=api)
        out = apc.run(["model", "deepseek", "flash", "4.1"], orouter=orouter)
        self.assertEqual(out["id"], "deepseek/deepseek-v4.1-flash")
        self.assertIsNone(api.calls[-1][2])  # the model list needs no key
        out = apc.run(["model", "ds", "--aliases", json.dumps({"DS": "openai/gpt-5-mini"})], orouter=orouter)
        self.assertEqual(out["id"], "openai/gpt-5-mini")

    def test_run_writes_out_file(self):
        wt = tempfile.mkdtemp()
        out_path = os.path.join(tempfile.mkdtemp(), "coder.json")
        orouter = apc.OpenRouter(keychain=FakeKeychain(), request=FakeAPI())
        with mock.patch.object(apc, "run_coder", return_value={"ok": True, "cost_usd": 0.1}), mock.patch("sys.stdin", mock.Mock(read=lambda: "x")):
            apc.run(["run", "--model", "openai/gpt-5", "--cwd", wt, "--max-usd", "1", "--out", out_path], orouter=orouter)
        self.assertEqual(json.loads(pathlib.Path(out_path).read_text())["cost_usd"], 0.1)

    def test_coding_outside_api_mode_is_booked_and_capped_by_the_day(self):
        apc.add_spend(19.5, "coder")  # default usd_per_day is 20
        orouter = apc.OpenRouter(keychain=FakeKeychain(), request=FakeAPI())
        seen = {}

        def fake_run(orouter, model, cwd, prompt, max_usd, timeout_min, book=None):
            seen["max_usd"] = max_usd
            return {"ok": True, "cost_usd": 0.3}

        try:
            with mock.patch.object(apc, "run_coder", fake_run), mock.patch("sys.stdin", mock.Mock(read=lambda: "x")):
                out = apc.run(["run", "--model", "openai/gpt-5", "--cwd", ".", "--max-usd", "2"], orouter=orouter)
                self.assertEqual(seen["max_usd"], 0.5)  # what's left of the day, not the 2 asked for
                self.assertEqual(out["spent_today_usd"], 19.8)
                apc.add_spend(1, "coder")
                with self.assertRaisesRegex(apc.CoderError, "budget"):
                    apc.run(["run", "--model", "openai/gpt-5", "--cwd", ".", "--max-usd", "2"], orouter=orouter)
        finally:
            os.remove(apc.LEDGER)

    def test_a_damaged_ledger_fails_closed(self):
        os.makedirs(os.path.dirname(apc.LEDGER), exist_ok=True)
        pathlib.Path(apc.LEDGER).write_text("{not json")
        try:
            with self.assertRaisesRegex(apc.LinearError, "ledger"):
                apc.day_left()
            with self.assertRaisesRegex(apc.LinearError, "ledger"):
                apc.add_spend(1, "coder")
        finally:
            os.remove(apc.LEDGER)

    def test_concurrent_bookings_all_count(self):
        import threading
        threads = [threading.Thread(target=apc.add_spend, args=(0.25, "coder")) for _ in range(20)]
        try:
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            self.assertEqual(apc.spent_today(), 5.0)
        finally:
            os.remove(apc.LEDGER)

    def test_a_run_killed_mid_way_still_books_its_spend(self):
        wt = tempfile.mkdtemp()
        os.environ["AUTOPILOT_CLAUDE_BIN"] = "/usr/bin/true"
        orouter = apc.OpenRouter(keychain=FakeKeychain(), request=FakeAPI(usage=(1.0, 1.0, 1.4)))
        booked, proc = [], FakeProc([None] * 5)
        try:
            with self.assertRaises(SystemExit):
                apc.run_coder(orouter, "openai/gpt-5", wt, "x", 1, 60, popen=lambda a, **kw: proc.started(kw["stdout"]),
                              sleep=mock.Mock(side_effect=[None, SystemExit(1)]), book=booked.append)
        finally:
            os.environ.pop("AUTOPILOT_CLAUDE_BIN", None)
        self.assertTrue(proc.terminated)
        self.assertEqual([round(b, 2) for b in booked], [0.4])

    def test_config_set_validates_and_keeps_the_rest(self):
        try:
            self.assertEqual(apc.set_setting("usd_per_day", "30")["usd_per_day"], 30)
            self.assertEqual(apc.set_setting("coder", "deepseek/deepseek-v4.1-flash")["usd_per_day"], 30)
            for key, value in (("usd_per_day", "900"), ("usd_per_day", "abc"), ("reviewer", "anthropic/claude-opus-5.5"), ("budget", "1")):
                with self.subTest(key=key, value=value), self.assertRaises(apc.LinearError):
                    apc.set_setting(key, value)
            self.assertEqual(apc.settings()["coder"], "deepseek/deepseek-v4.1-flash")
        finally:
            os.remove(apc.CONFIG)

    def test_run_with_no_budget_left_refuses(self):
        orouter = apc.OpenRouter(keychain=FakeKeychain(), request=FakeAPI())
        with self.assertRaisesRegex(apc.CoderError, "max-usd"):
            apc.run(["run", "--model", "openai/gpt-5", "--cwd", ".", "--max-usd", "0"], orouter=orouter)


if __name__ == "__main__":
    unittest.main()
