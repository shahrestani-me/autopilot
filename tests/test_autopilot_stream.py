"""Unit tests for bin/autopilot-stream. Stdlib only; nothing is posted to Linear."""

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("autopilot_stream", str(ROOT / "bin" / "autopilot-stream"))
_spec = importlib.util.spec_from_loader("autopilot_stream", _loader)
st = importlib.util.module_from_spec(_spec)
_loader.exec_module(st)


def assistant(text=None, tools=(), sidechain=False, ids=()):
    content = ([{"type": "text", "text": text}] if text else []) + [{"type": "tool_use", "name": t, "input": {}} for t in tools]
    content += [{"type": "tool_use", "id": i, "name": "Bash", "input": {}} for i in ids]
    return {"type": "assistant", "isSidechain": sidechain, "message": {"role": "assistant", "content": content}}


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        st.STATE_DIR = self.tmp
        self.transcript = os.path.join(self.tmp, "t.jsonl")
        open(self.transcript, "w").close()
        self.posted = []

    def write(self, *entries):
        with open(self.transcript, "a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def event(self, kind="PostToolUse", tool="Bash", command="ls", stdout="", **extra):
        e = {"session_id": "sess-abc", "transcript_path": self.transcript, "hook_event_name": kind}
        if kind == "PostToolUse":
            e.update(tool_name=tool, tool_input={"command": command} if tool == "Bash" else extra.pop("tool_input", {}),
                     tool_response={"stdout": stdout, "stderr": ""} if tool == "Bash" else extra.pop("tool_response", None))
        e.update(extra)
        return e

    def post(self, issue, content, state=None):
        self.posted.append((issue, content))
        return content

    def start(self, announce=False):
        """The run takes its issue lock. Unless `announce`, the "Run by …" line is taken as posted."""
        self.write(assistant("Before the run took a lock: not mirrored."))
        st.handle(self.event(command='"$AI" lock ENG-7', stdout='{\n  "locked": true,\n  "name": "ENG-7",\n  "token": "x"\n}'), self.post)
        state = st.read_state("sess-abc")
        if state and not announce:
            state.pop("announce")
            st.write_state("sess-abc", state)

    def run_record(self, rec):
        os.makedirs(os.path.join(self.tmp, "runs"), exist_ok=True)
        with open(os.path.join(self.tmp, "runs", "sess-abc.json"), "w") as f:
            json.dump(rec, f)

    def test_other_sessions_are_left_alone(self):
        self.write(assistant("hello"))
        st.handle(self.event(command="git status", stdout="clean"), self.post)
        st.handle(self.event(command='"$AI" lock ENG-7', stdout='{"locked": false, "name": "ENG-7"}'), self.post)
        st.handle(self.event(command='"$AI" lock verify', stdout='{"locked": true, "name": "verify"}'), self.post)
        self.assertEqual(self.posted, [])
        self.assertIsNone(st.read_state("sess-abc"))

    def test_a_run_is_mirrored_from_its_issue_lock_until_unlock(self):
        self.start()
        self.assertEqual(self.posted, [])  # nothing from before the lock
        self.write(assistant("Reading the ticket.", tools=["Bash"]))
        st.handle(self.event(command="gh pr view 12 -R o/r --json state", stdout='{"state":"OPEN"}'), self.post)
        self.assertEqual(self.posted, [
            ("ENG-7", {"type": "thought", "body": "Reading the ticket."}),
            ("ENG-7", {"type": "action", "action": "Bash", "parameter": "gh pr view 12 -R o/r --json state", "result": '{"state":"OPEN"}'}),
        ])
        self.posted.clear()
        st.handle(self.event(command='"$AI" unlock ENG-7 --token x', stdout='{"unlocked": "ENG-7"}'), self.post)
        self.assertIsNone(st.read_state("sess-abc"))
        st.handle(self.event(command="echo after", stdout="after"), self.post)
        self.assertEqual(self.posted, [])

    def test_text_is_posted_once_and_subagents_are_skipped(self):
        self.start()
        self.write(assistant("one"), assistant("from a subagent", sidechain=True))
        st.handle(self.event(tool="Read", tool_input={"file_path": "/x/a.py"}, tool_response={"file": {"numLines": 12}}), self.post)
        st.handle(self.event(tool="Read", tool_input={"file_path": "/x/b.py"}, tool_response={}), self.post)
        bodies = [c.get("body") for _, c in self.posted if c["type"] == "thought"]
        self.assertEqual(bodies, ["one"])
        self.assertEqual([c["parameter"] for _, c in self.posted if c["type"] == "action"], ["/x/a.py", "/x/b.py"])
        self.assertEqual(self.posted[1][1]["result"], "12 lines")

    def test_a_half_written_line_waits_for_the_next_call(self):
        self.start()
        with open(self.transcript, "a") as f:
            f.write(json.dumps(assistant("complete")) + "\n" + json.dumps(assistant("partial"))[:20])
        st.handle(self.event(tool="Glob", tool_input={"pattern": "*.py"}, tool_response=[]), self.post)
        self.assertEqual([c.get("body") for _, c in self.posted if c["type"] == "thought"], ["complete"])

    def test_own_linear_posts_and_lock_chores_are_not_echoed(self):
        self.start()
        for cmd in ('"$AP" activity ENG-7 --type thought --body hi', '"$AI" slot touch 1 --token t', '"$AI" lock ENG-7 --touch --token x',
                    '~/.claude/plugins/cache/autopilot/autopilot/0.8.0/bin/autopilot-linear comment ENG-7 --body x'):
            st.handle(self.event(command=cmd, stdout="{}"), self.post)
        st.handle(self.event(tool="ToolSearch", tool_input={"query": "x"}), self.post)
        self.assertEqual(self.posted, [])

    def test_stop_flushes_and_ends(self):
        self.start()
        self.write(assistant("Done: PR merged."))
        st.handle(self.event(kind="Stop"), self.post)
        self.assertEqual(self.posted, [("ENG-7", {"type": "thought", "body": "Done: PR merged."})])
        self.assertIsNone(st.read_state("sess-abc"))

    def test_secrets_are_redacted_and_long_text_trimmed(self):
        text = "GH_TOKEN=ghs_abcdefghijklmnopqrstuvwxyz0123 and Authorization: Bearer abcdefghijklmnopqrstuvwxyz and eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefghijk"
        out = st.redact(text)
        self.assertNotIn("ghs_abcdef", out)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz and", out)
        self.assertIn("[jwt]", out)
        self.assertEqual(len(st.trim("x" * 5000, 100)), 100)
        self.assertEqual(st.one_line("a\n\n b\n", 50), "a ⏎ b")

    def test_a_failed_post_is_logged_and_the_rest_still_go(self):
        self.start()
        self.write(assistant("first"), assistant("second"))
        calls = []

        def flaky(issue, content, state):
            calls.append(content)
            if content.get("body") == "first":
                raise RuntimeError("Linear down")
            return content
        st.handle(self.event(command="ls", stdout="a"), flaky)
        self.assertEqual([c.get("body") or c["action"] for c in calls], ["first", "second", "Bash"])
        self.assertIn("Linear down", open(os.path.join(self.tmp, "stream.log")).read())
        calls.clear()
        st.handle(self.event(command="ls", stdout="a"), flaky)
        self.assertEqual([c["type"] for c in calls], ["action"])  # no re-send of the thoughts

    def test_off_switch_and_bad_session_ids(self):
        os.environ["AUTOPILOT_STREAM"] = "0"
        try:
            self.start()
            self.assertIsNone(st.read_state("sess-abc"))
        finally:
            del os.environ["AUTOPILOT_STREAM"]
        e = self.event(command='"$AI" lock ENG-7', stdout='{"locked": true, "name": "ENG-7"}')
        e["session_id"] = "../../etc"
        st.handle(e, self.post)
        self.assertEqual(os.listdir(self.tmp), ["t.jsonl"])

    def test_a_lock_in_a_chained_command_still_starts_the_mirror(self):
        out = '{\n  "locked": true,\n  "name": "ENG-9",\n  "token": "t"\n}\n{"id": "job", "status": "running"}\n'
        st.handle(self.event(command='"$AI" lock ENG-9 && "$AP" jobs ack x', stdout=out), self.post)
        self.assertEqual(st.read_state("sess-abc")["issue"], "ENG-9")

    def test_the_runs_final_word_ends_the_mirror(self):
        self.start()
        self.write(assistant("Merged and verified."))
        st.handle(self.event(command='"$AP" activity ENG-7 --type response --body-file - <<\'EOF\'', stdout="{}"), self.post)
        self.assertIsNone(st.read_state("sess-abc"))
        self.assertEqual(self.posted, [("ENG-7", {"type": "thought", "body": "Merged and verified."})])
        self.write(assistant("Writing the report."))
        st.handle(self.event(tool="Write", tool_input={"file_path": "/r.md"}), self.post)
        st.handle(self.event(kind="Stop"), self.post)
        self.assertEqual(len(self.posted), 1)  # nothing reopens the closed session

    def test_a_subagents_tool_calls_are_left_out(self):
        self.start()
        self.write(assistant("Running review.", ids=["tu_main"]))
        st.handle(self.event(command="git diff", stdout="diff", tool_use_id="tu_main"), self.post)
        st.handle(self.event(command="grep -r x", stdout="hits", tool_use_id="tu_sub"), self.post)  # a reviewer's
        self.assertEqual([c.get("parameter") for _, c in self.posted if c["type"] == "action"], ["git diff"])

    def test_secret_output_is_hidden_and_more_formats_redacted(self):
        d = st.describe_tool("Bash", {"command": "security find-generic-password -s x -w"}, {"stdout": "s3cretValue"})
        self.assertEqual(d[2], "(output hidden)")
        out = st.redact("key AIzaSyA1234567890abcdefghijklmnopqrstuv cf Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4zAb7c sha 3f9a1c2e4b5d6f708192a3b4c5d6e7f809112233")
        self.assertNotIn("AIzaSy", out)
        self.assertNotIn("Ab3dEf6hIj9k", out)
        self.assertIn("3f9a1c2e4b5d6f708192a3b4c5d6e7f809112233", out)  # git SHAs stay readable

    def test_a_rewritten_transcript_does_not_silence_the_mirror(self):
        self.start()
        state = st.read_state("sess-abc")
        state["offset"] = 10_000_000
        st.write_state("sess-abc", state)
        st.handle(self.event(command="ls", stdout="a"), self.post)
        self.write(assistant("still here"))
        st.handle(self.event(command="ls", stdout="a"), self.post)
        self.assertIn(("ENG-7", {"type": "thought", "body": "still here"}), self.posted)

    def test_empty_results_are_never_sent(self):
        self.start()
        st.handle(self.event(tool="Grep", tool_input={"pattern": "nope"}, tool_response=""), self.post)
        self.assertEqual(self.posted[-1][1]["result"], "-")

    def test_offset_is_saved_after_each_post(self):
        self.start()
        self.write(assistant("a"), assistant("b"))
        seen = []

        def dies_on_b(issue, content, state):
            if content.get("body") == "b":
                raise KeyboardInterrupt  # the hook killed by its timeout
            seen.append(content.get("body"))
            return content
        with self.assertRaises(KeyboardInterrupt):
            st.handle(self.event(command="ls", stdout="x"), dies_on_b)
        st.handle(self.event(command="ls", stdout="x"), self.post)
        self.assertEqual([c.get("body") for _, c in self.posted if c["type"] == "thought"], ["b"])  # "a" not again

    def test_the_agent_that_runs_it_heads_the_session(self):
        self.run_record({"slot": 1, "slot_token": "t", "agent": "autopilot-agent-3"})
        self.start(announce=True)
        self.assertEqual(self.posted, [])  # nothing posted at the lock itself
        self.write(assistant("Reading the ticket."))
        st.handle(self.event(command="ls", stdout="a"), self.post)
        self.assertEqual([c.get("body") or c["action"] for _, c in self.posted],
                         ["Run by Agent 3 (`autopilot-agent-3`) · Claude session `sess-abc`", "Reading the ticket.", "Bash"])
        self.posted.clear()
        st.handle(self.event(command="ls", stdout="a"), self.post)
        self.assertEqual([c["type"] for _, c in self.posted], ["action"])  # once per run

    def test_the_agent_is_named_whichever_slot_it_took(self):
        st.handle(self.event(command='"$AI" slot take --agent autopilot-agent && "$AI" lock ENG-7',
                             stdout='{"slot": 3, "token": "t", "max": 3, "agent": "autopilot-agent"}\n{"locked": true, "name": "ENG-7", "token": "x"}'), self.post)
        st.handle(self.event(kind="Stop"), self.post)
        self.assertEqual(self.posted, [("ENG-7", {"type": "thought", "body": "Run by Agent 1 (`autopilot-agent`) · Claude session `sess-abc`"})])

    def test_a_manual_run_says_so(self):
        self.start(announce=True)
        st.handle(self.event(kind="Stop"), self.post)
        self.assertEqual(self.posted[0][1]["body"], "Manual run (no scheduled agent) · Claude session `sess-abc`")

    def test_a_session_it_opens_is_kept_for_the_rest_of_the_run(self):
        # ENG-67: with no open session and no claimed job, the first post opens one. It used to be
        # lost from the state, so each later post opened another.
        opened, sent = [], []

        class FakeLinear:
            def __init__(self, expect_session=None):
                pass

            def ensure_session(self, issue):
                opened.append(issue)
                return f"linear-sess-{len(opened)}"

            def activity(self, issue, content, ephemeral, session_id):
                sent.append(session_id)

        class FakeApl:
            Linear = FakeLinear
            Gateway = object

            @staticmethod
            def expect_own_session(gateway):
                return None

            @staticmethod
            def read_current_job(issue):
                return None

        real_load = st._load
        st._load = lambda name: FakeApl if name == "autopilot-linear" else real_load(name)
        self.addCleanup(setattr, st, "_load", real_load)
        self.start(announce=True)
        post = st._post_via_linear("sess-abc")
        for text in ("one", "two"):
            self.write(assistant(text))
            st.handle(self.event(command="ls", stdout="x"), post)
        self.assertEqual(opened, ["ENG-7"])
        self.assertEqual(set(sent), {"linear-sess-1"})
        self.assertEqual(len(sent), 5)  # the "Run by" line, two thoughts, two actions
        self.assertEqual(st.read_state("sess-abc")["linearSession"], "linear-sess-1")

    def test_mcp_and_other_tools_get_a_compact_line(self):
        d = st.describe_tool("mcp__claude-in-chrome__navigate", {"url": "https://dev.example"}, {"ok": True})
        self.assertEqual(d[0], "navigate (chrome)")
        self.assertIn("dev.example", d[1])
        self.assertEqual(st.describe_tool("Skill", {"skill": "code-review", "args": "high 5"}, None)[:2], ("Skill", "code-review high 5"))


if __name__ == "__main__":
    unittest.main()
