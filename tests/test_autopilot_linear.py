"""Unit tests for bin/autopilot-linear. Stdlib only; no network, no Keychain."""

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
import urllib.parse
from unittest import mock

# Hermetic: never reach the real Keychain's gateway setup from tests (GatewayTests opt in).
os.environ["AUTOPILOT_GATEWAY"] = "off"

ROOT = pathlib.Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("autopilot_linear", str(ROOT / "bin" / "autopilot-linear"))
_spec = importlib.util.spec_from_loader("autopilot_linear", _loader)
apl = importlib.util.module_from_spec(_spec)
_loader.exec_module(apl)

NOW = 1_800_000_000
APP_ID = "app-user-1"


class FakeKeychain:
    def __init__(self, items=None, comments=None):
        self.items = dict(items or {})
        self.comments = dict(comments or {})
        self.writes = []

    def get(self, account):
        return self.items.get(account)

    def get_comment(self, account):
        return self.comments.get(account)

    def set(self, account, secret, comment):
        self.items[account] = secret
        self.comments[account] = comment
        self.writes.append(account)


class FakeHTTP:
    """Records requests; answers the token endpoint and GraphQL by operation."""

    def __init__(self, graphql=None, token_status=200, reject_tokens=()):
        self.calls = []
        self.graphql = graphql or (lambda q, v: {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "autopilot", "app": True}})
        self.token_status = token_status
        self.reject_tokens = set(reject_tokens)
        self.minted = 0

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        if url == apl.TOKEN_URL:
            if self.token_status != 200:
                return self.token_status, {"error": "invalid_client"}
            self.minted += 1
            return 200, {"access_token": f"lin_oauth_minted{self.minted}", "expires_in": 2591999}
        token = headers["Authorization"].split(" ", 1)[1]
        if token in self.reject_tokens:
            return 401, {"errors": [{"message": "auth", "extensions": {"code": "AUTHENTICATION_ERROR"}}]}
        req = json.loads(body)
        return 200, {"data": self.graphql(req["query"], req["variables"])}


def client(kc=None, http=None):
    return apl.Linear(keychain=kc or FakeKeychain(), post=http or FakeHTTP(), now=lambda: NOW)


def creds(**extra):
    return FakeKeychain({"client-id": "cid", "client-secret": "csecret", **extra})


class TokenTests(unittest.TestCase):
    def test_missing_setup_is_a_setup_error(self):
        with self.assertRaises(apl.SetupError):
            client(FakeKeychain()).token()

    def test_mints_with_client_credentials_and_caches_in_keychain(self):
        kc, http = creds(), FakeHTTP()
        tok = client(kc, http).token()
        self.assertEqual(tok, "lin_oauth_minted1")
        url, body, _ = http.calls[0]
        form = urllib.parse.parse_qs(body.decode())
        self.assertEqual(url, apl.TOKEN_URL)
        self.assertEqual(form["grant_type"], ["client_credentials"])
        self.assertEqual(form["scope"], ["read,write,app:assignable,app:mentionable"])
        self.assertEqual(kc.items["access-token"], tok)
        self.assertEqual(kc.comments["access-token"], f"exp={NOW + 2591999}")

    def test_uses_cached_token_until_a_day_before_expiry(self):
        kc = creds(**{"access-token": "lin_oauth_cached"})
        kc.comments["access-token"] = f"exp={NOW + 3 * 86400}"
        http = FakeHTTP()
        self.assertEqual(client(kc, http).token(), "lin_oauth_cached")
        self.assertEqual(http.minted, 0)

    def test_remints_when_cached_token_is_near_expiry(self):
        kc = creds(**{"access-token": "lin_oauth_old"})
        kc.comments["access-token"] = f"exp={NOW + 3600}"
        http = FakeHTTP()
        self.assertEqual(client(kc, http).token(), "lin_oauth_minted1")

    def test_hand_stored_token_without_expiry_is_replaced_by_a_minted_one(self):
        # A token without exp= may be a personal key that would post as the owner.
        kc = creds(**{"access-token": "lin_api_personal"})
        self.assertEqual(client(kc).token(), "lin_oauth_minted1")

    def test_refuses_a_token_that_acts_as_a_human(self):
        http = FakeHTTP(graphql=lambda q, v: {"viewer": {"id": "u", "name": "Owner", "displayName": "o", "app": False}})
        with self.assertRaises(apl.AuthError):
            client(creds(), http).viewer()

    def test_keychain_write_failure_still_uses_the_minted_token(self):
        class BrokenKeychain(FakeKeychain):
            def set(self, *a):
                raise apl.LinearError("nope")
        self.assertEqual(client(BrokenKeychain({"client-id": "c", "client-secret": "s"})).token(), "lin_oauth_minted1")

    def test_retries_transient_failures_with_backoff(self):
        replies = [(503, None), (0, None), (200, {"errors": [{"message": "slow", "extensions": {"code": "RATELIMITED"}}]})]
        inner = FakeHTTP()
        sleeps = []

        def post(url, body, headers):
            if url == apl.API_URL and replies:
                return replies.pop(0)
            return inner(url, body, headers)

        c = apl.Linear(keychain=creds(), post=post, now=lambda: NOW, sleep=sleeps.append)
        self.assertEqual(c.viewer()["id"], APP_ID)
        self.assertEqual(sleeps, [2, 4, 8])

    def test_writes_are_not_retried_after_a_5xx_or_timeout(self):
        # The server may already have created the comment; a retry would duplicate it.
        posts = []

        def post(url, body, headers):
            if url == apl.API_URL and b"commentCreate" in body:
                posts.append(body)
                return 0, None
            return FakeHTTP()(url, body, headers)

        c = apl.Linear(keychain=creds(), post=post, now=lambda: NOW, sleep=lambda s: None)
        with self.assertRaises(apl.LinearError):
            c.comment("ENG-1", "hi")
        self.assertEqual(len(posts), 1)

    def test_writes_are_retried_on_rate_limit(self):
        replies = [(429, None)]

        def post(url, body, headers):
            if url == apl.API_URL and b"commentCreate" in body:
                if replies:
                    return replies.pop()
                return 200, {"data": {"commentCreate": {"success": True, "comment": {"id": "c", "url": "u", "user": {"name": "Autopilot", "app": True}}}}}
            return FakeHTTP()(url, body, headers)

        c = apl.Linear(keychain=creds(), post=post, now=lambda: NOW, sleep=lambda s: None)
        self.assertEqual(c.comment("ENG-1", "hi")["id"], "c")

    def test_gives_up_after_retries(self):
        c = apl.Linear(keychain=creds(), post=lambda u, b, h: (502, None) if u == apl.API_URL else FakeHTTP()(u, b, h),
                       now=lambda: NOW, sleep=lambda s: None)
        with self.assertRaises(apl.LinearError):
            c.viewer()

    def test_401_remints_once_and_retries(self):
        kc = creds(**{"access-token": "lin_oauth_revoked"})
        kc.comments["access-token"] = f"exp={NOW + 10 * 86400}"
        http = FakeHTTP(reject_tokens={"lin_oauth_revoked"})
        v = client(kc, http).viewer()
        self.assertEqual(v["id"], APP_ID)
        self.assertEqual(http.minted, 1)

    def test_persistent_401_raises_auth_error(self):
        http = FakeHTTP(reject_tokens={"lin_oauth_minted1", "lin_oauth_minted2"})
        with self.assertRaises(apl.AuthError):
            client(creds(), http).viewer()

    def test_token_failure_message_has_no_secret(self):
        with self.assertRaises(apl.AuthError) as cm:
            client(creds(), FakeHTTP(token_status=401)).token()
        self.assertNotIn("csecret", str(cm.exception))

    def test_missing_expires_in_assumes_thirty_days(self):
        kc = creds()
        http = FakeHTTP()
        http_no_exp = lambda u, b, h: (200, {"access_token": "lin_oauth_x"}) if u == apl.TOKEN_URL else http(u, b, h)
        client(kc, http_no_exp).token()
        self.assertEqual(kc.comments["access-token"], f"exp={NOW + 30 * 86400}")

    def test_token_never_appears_in_cli_output(self):
        kc = creds()
        out = apl.run(["auth-check"], client=client(kc))
        self.assertTrue(out["ok"])
        self.assertNotIn("lin_oauth", json.dumps(out))


def node(ident, priority=3, created="2026-10-01T00:00:00Z", state="unstarted", sessions=(), comments=(), children=()):
    return {
        "id": f"uuid-{ident}", "identifier": ident, "title": f"t {ident}", "url": f"https://linear.app/x/{ident}",
        "priority": priority, "createdAt": created,
        "state": {"name": state.title(), "type": state}, "team": {"key": "ENG"}, "project": {"name": "Autopilot Sandbox"},
        "labels": {"nodes": []},
        "children": {"nodes": [{"identifier": c} for c in children]},
        "agentSessions": {"nodes": list(sessions)},
        "comments": {"nodes": list(comments)},
    }


def session(status, updated="2026-10-02T00:00:00Z", app=APP_ID):
    return {"id": f"s-{status}", "status": status, "createdAt": "2026-10-01T12:00:00Z", "updatedAt": updated, "appUser": {"id": app}}


def human(at):
    return {"createdAt": at, "user": {"id": "owner", "app": False}}


class QueueTests(unittest.TestCase):
    def run_queue(self, nodes):
        seen = {}

        def gql(q, v):
            if "viewer" in q and "issues" not in q:
                return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "autopilot", "app": True}}
            seen["filter"] = v["filter"]
            return {"issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}

        q = client(creds(), FakeHTTP(graphql=gql)).queue(["Autopilot Sandbox"], "ENG")
        return q, seen["filter"]

    def test_filters_on_delegate_open_states_project_and_team(self):
        _, flt = self.run_queue([])
        self.assertEqual(flt["delegate"], {"id": {"eq": APP_ID}})
        self.assertEqual(flt["state"], {"type": {"nin": ["completed", "canceled"]}})
        self.assertEqual(flt["project"], {"name": {"in": ["Autopilot Sandbox"]}})
        self.assertEqual(flt["team"], {"key": {"eq": "ENG"}})

    def test_orders_resume_then_priority_with_none_last_then_oldest(self):
        q, _ = self.run_queue([
            node("A", priority=0),
            node("B", priority=3, created="2026-10-05T00:00:00Z"),
            node("C", priority=3, created="2026-10-01T00:00:00Z"),
            node("D", priority=1),
            node("E", priority=4, state="started"),
        ])
        self.assertEqual([i["identifier"] for i in q["issues"]], ["E", "D", "C", "B", "A"])
        self.assertEqual(q["count"], 5)

    def test_holds_issue_awaiting_owner_until_a_human_replies(self):
        q, _ = self.run_queue([
            node("WAIT", sessions=[session("awaitingInput")], comments=[human("2026-10-01T23:00:00Z")]),
            node("REPLIED", sessions=[session("awaitingInput")], comments=[human("2026-10-03T00:00:00Z")]),
            node("ERR", sessions=[session("error")]),
        ])
        self.assertEqual([i["identifier"] for i in q["issues"]], ["REPLIED"])
        self.assertEqual(q["issues"][0]["lastHumanAt"], "2026-10-03T00:00:00Z")
        self.assertEqual({i["identifier"] for i in q["held"]}, {"WAIT", "ERR"})

    def test_app_comments_do_not_count_as_a_reply(self):
        bot = {"createdAt": "2026-10-03T00:00:00Z", "user": {"id": APP_ID, "app": True}}
        q, _ = self.run_queue([node("W", sessions=[session("awaitingInput")], comments=[bot])])
        self.assertEqual(q["count"], 0)

    def test_other_agents_sessions_are_ignored(self):
        q, _ = self.run_queue([node("X", sessions=[session("awaitingInput", app="other-agent")])])
        self.assertEqual(q["count"], 1)

    def test_exclude_project_filter(self):
        seen = {}

        def gql(q, v):
            if "issues" not in q:
                return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
            seen["f"] = v["filter"]
            return {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}

        apl.run(["queue", "--exclude-project", "Web App"], client=client(creds(), FakeHTTP(graphql=gql)))
        self.assertEqual(seen["f"]["project"], {"name": {"nin": ["Web App"]}})

    def test_paginates(self):
        pages = [
            {"nodes": [node("P1")], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}},
            {"nodes": [node("P2", priority=1)], "pageInfo": {"hasNextPage": False, "endCursor": None}},
        ]
        afters = []

        def gql(q, v):
            if "issues" not in q:
                return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
            afters.append(v["after"])
            return {"issues": pages.pop(0)}

        q = client(creds(), FakeHTTP(graphql=gql)).queue()
        self.assertEqual(afters, [None, "c1"])
        self.assertEqual([i["identifier"] for i in q["issues"]], ["P2", "P1"])

    def test_holds_issue_whose_last_autopilot_comment_asks_the_owner(self):
        ask = {"createdAt": "2026-10-02T00:00:00Z", "body": apl.FALLBACK_PREFIX["elicitation"] + "Which option?",
               "user": {"id": APP_ID, "app": True}}
        q, _ = self.run_queue([
            node("ASKED", comments=[ask]),
            node("ANSWERED", comments=[ask, human("2026-10-03T00:00:00Z")]),
            node("OLDREPLY", comments=[human("2026-10-01T00:00:00Z"), ask]),
        ])
        self.assertEqual([i["identifier"] for i in q["issues"]], ["ANSWERED"])
        self.assertIn("asked in a comment", {i["identifier"]: i for i in q["held"]}["ASKED"]["heldBecause"])

    def test_later_progress_comment_does_not_release_the_hold(self):
        ask = {"createdAt": "2026-10-02T00:00:00Z", "body": apl.FALLBACK_PREFIX["error"] + "Started twice.",
               "user": {"id": APP_ID, "app": True}}
        later = {"createdAt": "2026-10-02T01:00:00Z", "body": "🛠 Plan: ...", "user": {"id": APP_ID, "app": True}}
        q, _ = self.run_queue([node("E", comments=[later, ask])])
        self.assertEqual(q["count"], 0)

    def test_marker_must_start_the_comment(self):
        mid = {"createdAt": "2026-10-02T00:00:00Z", "body": "Note: " + apl.FALLBACK_PREFIX["elicitation"],
               "user": {"id": APP_ID, "app": True}}
        q, _ = self.run_queue([node("M", comments=[mid])])
        self.assertEqual(q["count"], 1)

    def test_marker_from_someone_else_does_not_hold(self):
        fake = {"createdAt": "2026-10-02T00:00:00Z", "body": apl.FALLBACK_PREFIX["elicitation"] + "x",
                "user": {"id": "owner", "app": False}}
        q, _ = self.run_queue([node("F", comments=[fake])])
        self.assertEqual(q["count"], 1)

    def test_plain_autopilot_comment_does_not_hold(self):
        note = {"createdAt": "2026-10-02T00:00:00Z", "body": "🤖 Autopilot picked this up", "user": {"id": APP_ID, "app": True}}
        q, _ = self.run_queue([node("N", comments=[note])])
        self.assertEqual(q["count"], 1)

    def test_holds_issue_with_open_children(self):
        q, _ = self.run_queue([node("P", children=["ENG-9"])])
        self.assertEqual(q["count"], 0)
        self.assertIn("ENG-9", q["held"][0]["heldBecause"])

    def test_cli_next_and_count(self):
        def gql(q, v):
            if "issues" not in q:
                return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
            return {"issues": {"nodes": [node("N1", priority=2)], "pageInfo": {"hasNextPage": False, "endCursor": None}}}

        c = client(creds(), FakeHTTP(graphql=gql))
        self.assertEqual(apl.run(["queue", "--count"], client=c), 1)
        self.assertEqual(apl.run(["queue", "--next"], client=c)["identifier"], "N1")


class UsersTests(unittest.TestCase):
    def test_lists_active_people_without_email(self):
        def gql(q, v):
            return {"users": {"nodes": [
                {"id": "u1", "name": "Jane Doe", "displayName": "jane", "active": True, "email": "j@example.com"},
                {"id": "u2", "name": "Gone", "displayName": "gone", "active": False}]}}
        out = apl.run(["users"], client=client(creds(), FakeHTTP(graphql=gql)))
        self.assertEqual(out, [{"id": "u1", "name": "Jane Doe", "displayName": "jane"}])


class TLSTests(unittest.TestCase):
    def test_falls_back_to_system_ca_bundle_when_python_has_none(self):
        loaded = []

        class Ctx:
            def get_ca_certs(self):
                return []

            def load_verify_locations(self, path):
                loaded.append(path)

        orig = apl.ssl.create_default_context
        apl.ssl.create_default_context = lambda: Ctx()
        try:
            apl.ssl_context(ca_file=__file__)
        finally:
            apl.ssl.create_default_context = orig
        self.assertEqual(loaded, [__file__])

    def test_network_error_reason_is_reported(self):
        c = apl.Linear(keychain=creds(), post=lambda u, b, h: (0, {"networkError": "CERTIFICATE_VERIFY_FAILED"}),
                       now=lambda: NOW, sleep=lambda s: None)
        with self.assertRaises(apl.LinearError) as cm:
            c.token()
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(cm.exception))


class RealKeychainCommandTests(unittest.TestCase):
    """The Keychain class with subprocess faked: argv, stdin and exit codes."""

    def make(self, store):
        calls = []

        def run(argv, input=None, **kw):
            calls.append((argv, input))
            class R: pass
            r = R(); r.stdout = r.stderr = ""; r.returncode = 0
            if argv[1] == "-i":
                acct = input.split(" -a ")[1].split()[0]
                store[acct] = (input.split('-w "')[1].split('"')[0], input.split('-j "')[1].split('"')[0])
            else:
                acct = argv[argv.index("-a") + 1]
                if acct not in store:
                    r.returncode = 44
                elif "-w" in argv:
                    r.stdout = store[acct][0] + "\n"
                else:
                    r.stdout = f'attributes:\n    "icmt"<blob>="{store[acct][1]}"\n'
            return r

        return apl.Keychain(run=run), calls

    def test_secret_goes_on_stdin_never_argv(self):
        kc, calls = self.make({})
        kc.set("access-token", "lin_oauth_abc", "exp=5")
        argv, stdin = calls[0]
        self.assertEqual(argv, ["security", "-i"])
        self.assertIn('-w "lin_oauth_abc"', stdin)
        self.assertFalse(any("lin_oauth_abc" in " ".join(a) for a, _ in calls))

    def test_reads_value_and_comment_and_not_found(self):
        kc, _ = self.make({"access-token": ("tok", "exp=9")})
        self.assertEqual(kc.get("access-token"), "tok")
        self.assertEqual(kc.get_comment("access-token"), "exp=9")
        self.assertIsNone(kc.get("client-secret"))

    def test_other_security_errors_are_not_treated_as_missing(self):
        def run(argv, **kw):
            class R: returncode = 36; stdout = ""; stderr = "User interaction is not allowed."
            return R()
        with self.assertRaises(apl.LinearError):
            apl.Keychain(run=run).get("client-secret")

    def test_timeout_is_an_error_not_a_hang(self):
        def run(argv, **kw):
            raise apl.subprocess.TimeoutExpired(argv, kw.get("timeout"))
        with self.assertRaises(apl.LinearError):
            apl.Keychain(run=run).get("client-id")

    def test_rejects_values_with_quotes_or_newlines(self):
        kc, calls = self.make({})
        for bad in ('tok"; delete-keychain', "tok\n", "tok en"):
            with self.assertRaises(apl.LinearError):
                kc.set("access-token", bad, "exp=1")
        self.assertEqual(calls, [])

    def test_unverified_write_is_an_error(self):
        kc, _ = self.make({})
        kc.get = lambda account: None
        with self.assertRaises(apl.LinearError):
            kc.set("access-token", "tok", "exp=1")


class StatusGuardTests(unittest.TestCase):
    """ENG-205: Linear's PR automations overwrite Autopilot's statuses (merge → Done mid-run)."""

    STATES = [{"id": "ip", "name": "In Progress", "type": "started", "position": 1},
              {"id": "ir", "name": "In Review", "type": "started", "position": 2},
              {"id": "d", "name": "Done", "type": "completed", "position": 3}]

    def guard(self, after_wait):
        """A client where the issue reads `after_wait` once Autopilot has set its state."""
        self.updates, self.slept, reads = [], [], []

        def gql(q, v):
            if "viewer" in q:
                return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
            if "States" in q:
                reads.append(1)
                now = {"id": "ip", "name": "In Progress"} if len(reads) == 1 else after_wait
                return {"issue": {"id": "i1", "state": now, "team": {"key": "ENG", "states": {"nodes": self.STATES}}}}
            if "issueUpdate" in q:
                self.updates.append(v["input"]["stateId"])
                return {"issueUpdate": {"success": True, "issue": {"identifier": "ENG-1", "state": {"name": "In Review", "type": "started"}}}}
            raise AssertionError(q)
        c = client(creds(), FakeHTTP(graphql=gql))
        c.sleep = self.slept.append
        return c

    def test_a_status_linear_overwrote_is_set_again(self):
        out = apl.run(["state", "ENG-1", "In Review", "--settle", "20"], client=self.guard({"id": "d", "name": "Done"}))
        self.assertEqual(self.updates, ["ir", "ir"])
        self.assertEqual(self.slept, [20])
        self.assertEqual(out["reasserted"], {"from": "Done"})

    def test_a_status_that_held_is_left_alone(self):
        out = apl.run(["state", "ENG-1", "In Review", "--settle", "500"], client=self.guard({"id": "ir", "name": "In Review"}))
        self.assertEqual(self.updates, ["ir"])
        self.assertEqual(self.slept, [60])  # capped
        self.assertIs(out["reasserted"], False)

    def test_a_cancel_during_the_wait_is_respected(self):
        out = apl.run(["state", "ENG-1", "In Review", "--settle", "20"],
                      client=self.guard({"id": "c", "name": "Canceled", "type": "canceled"}))
        self.assertEqual(self.updates, ["ir"])
        self.assertEqual((out["reasserted"], out["now"]), (False, "Canceled"))

    def test_without_settle_nothing_waits(self):
        out = apl.run(["state", "ENG-1", "In Review"], client=self.guard({"id": "d", "name": "Done"}))
        self.assertEqual((self.updates, self.slept), (["ir"], []))
        self.assertNotIn("reasserted", out)

    def test_automations_lists_the_ones_that_move_issues(self):
        def gql(q, v):
            if "viewer" in q:
                return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
            nodes = {"ENG": [{"event": "merge", "state": {"name": "Done"}, "targetBranch": None},
                             {"event": "review", "state": None, "targetBranch": None}],
                     "OFF": []}[v["key"]]
            return {"teams": {"nodes": [{"key": v["key"], "gitAutomationStates": {"nodes": nodes}}]}}
        out = apl.run(["automations", "--team", "ENG", "--team", "OFF"], client=client(creds(), FakeHTTP(graphql=gql)))
        self.assertEqual(out, {"fights": True, "teams": [
            {"team": "ENG", "automations": [{"event": "merge", "state": "Done", "branch": None}]},
            {"team": "OFF", "automations": []}]})


class WriteTests(unittest.TestCase):
    def test_create_issue_resolves_team_project_and_state(self):
        def gql(q, v):
            if "viewer" in q:
                return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
            if "CreateContext" in q:
                return {"teams": {"nodes": [{"id": "t1", "states": {"nodes": [
                    {"id": "b", "name": "Backlog", "type": "backlog", "position": 0}]}}]},
                        "projects": {"nodes": [{"id": "p1"}]}}
            if "issueCreate" in q:
                self.assertEqual(v["input"], {"teamId": "t1", "projectId": "p1", "stateId": "b",
                                              "title": "T", "description": "D", "parentId": "ENG-1"})
                return {"issueCreate": {"success": True, "issue": {"identifier": "ENG-9", "url": "u"}}}
            raise AssertionError(q)

        out = apl.run(["create", "--team", "ENG", "--project", "P", "--title", "T", "--body", "D", "--parent", "ENG-1"],
                      client=client(creds(), FakeHTTP(graphql=gql)))
        self.assertEqual(out["identifier"], "ENG-9")

    def test_create_issue_assigns_exactly_one_human(self):
        def gql_for(users):
            def gql(q, v):
                if "viewer" in q:
                    return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
                if "CreateContext" in q:
                    return {"teams": {"nodes": [{"id": "t1", "states": {"nodes": [
                        {"id": "b", "name": "Backlog", "type": "backlog", "position": 0}]}}]},
                            "projects": {"nodes": [{"id": "p1"}]}}
                if "query User" in q:
                    self.assertEqual(v, {"q": "Owner Name"})
                    return {"users": {"nodes": users}}
                if "issueCreate" in q:
                    self.assertEqual(v["input"]["assigneeId"], "u1")
                    return {"issueCreate": {"success": True, "issue": {"identifier": "ENG-9", "url": "u"}}}
                raise AssertionError(q)
            return gql

        argv = ["create", "--team", "ENG", "--project", "P", "--title", "T", "--body", "D", "--assignee", "Owner Name"]
        out = apl.run(argv, client=client(creds(), FakeHTTP(graphql=gql_for(
            [{"id": APP_ID, "app": True}, {"id": "u1", "app": False}]))))
        self.assertEqual(out["identifier"], "ENG-9")
        for users in ([], [{"id": APP_ID, "app": True}], [{"id": "u1", "app": False}, {"id": "u2", "app": False}]):
            with self.assertRaises(apl.LinearError):
                apl.run(argv, client=client(creds(), FakeHTTP(graphql=gql_for(users))))

    def test_ask_owner_mentions_the_creator_else_the_fallback(self):
        def gql_for(creator, users=()):
            posted = []

            def gql(q, v):
                if "viewer" in q:
                    return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
                if "IssueCreator" in q:
                    return {"issue": {"identifier": "ENG-1", "creator": creator}}
                if "query User" in q:
                    self.assertEqual(v, {"q": "Owner Name"})
                    return {"users": {"nodes": list(users)}}
                if "commentCreate" in q:
                    posted.append(v["input"]["body"])
                    return {"commentCreate": {"success": True, "comment": {"id": "c1", "url": "u", "user": {"name": "Autopilot", "app": True}}}}
                raise AssertionError(q)
            return gql, posted

        human = {"name": "Jane", "url": "https://linear.app/w/profiles/k", "app": False}
        gql, posted = gql_for(human)
        out = apl.run(["ask-owner", "ENG-1", "--body", "Why: x", "--fallback", "Owner Name"],
                      client=client(creds(), FakeHTTP(graphql=gql)))
        self.assertEqual(out["mentioned"], "Jane")
        self.assertTrue(posted[0].startswith("https://linear.app/w/profiles/k "), posted[0])
        self.assertIn("Autopilot needs you on ENG-1", posted[0])
        self.assertIn("Why: x", posted[0])

        owner = {"id": "u1", "app": False, "name": "Owner", "url": "https://linear.app/w/profiles/o"}
        gql, posted = gql_for({"name": "Bot", "url": "https://linear.app/w/profiles/b", "app": True},
                              [{"id": APP_ID, "app": True, "name": "Autopilot", "url": "x"}, owner])
        out = apl.run(["ask-owner", "ENG-1", "--body", "Why: x", "--fallback", "Owner Name"],
                      client=client(creds(), FakeHTTP(graphql=gql)))
        self.assertEqual(out["mentioned"], "Owner")
        self.assertTrue(posted[0].startswith("https://linear.app/w/profiles/o "))

        gql, posted = gql_for(None)
        with self.assertRaises(apl.LinearError):
            apl.run(["ask-owner", "ENG-1", "--body", "Why: x"], client=client(creds(), FakeHTTP(graphql=gql)))
        self.assertEqual(posted, [])

    def test_activity_reuses_live_session_else_creates_one(self):
        calls = []

        def gql(q, v):
            calls.append(q)
            if "viewer" in q:
                return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
            if "query Sessions" in q:
                return {"issue": {"id": "uuid-1", "agentSessions": {"nodes": [
                    {"id": "done", "status": "complete", "createdAt": "2026-10-02", "dismissedAt": None, "appUser": {"id": APP_ID}},
                ]}}}
            if "agentSessionCreateOnIssue" in q:
                self.assertEqual(v["input"], {"issueId": "uuid-1"})
                return {"agentSessionCreateOnIssue": {"success": True, "agentSession": {"id": "new"}}}
            if "agentActivityCreate" in q:
                self.assertEqual(v["input"]["agentSessionId"], "new")
                self.assertEqual(v["input"]["content"], {"type": "thought", "body": "hi"})
                return {"agentActivityCreate": {"success": True, "agentActivity": {"id": "a1", "createdAt": "now"}}}
            raise AssertionError(q)

        out = apl.run(["activity", "ENG-1", "--type", "thought", "--body", "hi"], client=client(creds(), FakeHTTP(graphql=gql)))
        self.assertEqual(out["sessionId"], "new")
        self.assertTrue(any("agentSessionCreateOnIssue" in q for q in calls))

    def test_the_gateway_is_told_before_a_session_is_opened(self):
        order = []

        def gql(q, v):
            if "viewer" in q:
                return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
            if "query Sessions" in q:
                return {"issue": {"id": "uuid-1", "agentSessions": {"nodes": nodes}}}
            if "agentSessionCreateOnIssue" in q:
                order.append("create")
                return {"agentSessionCreateOnIssue": {"success": True, "agentSession": {"id": "new"}}}
            raise AssertionError(q)

        lin = apl.Linear(keychain=creds(), post=FakeHTTP(graphql=gql), now=lambda: NOW, sleep=lambda s: None,
                         expect_session=lambda issue_id: order.append(("expect", issue_id)))
        nodes = [{"id": "done", "status": "complete", "createdAt": "2026-10-02", "dismissedAt": None, "appUser": {"id": APP_ID}}]
        self.assertEqual(lin.ensure_session("ENG-1"), "new")
        self.assertEqual(order, [("expect", "uuid-1"), "create"])
        order.clear()
        nodes = [{"id": "live", "status": "active", "createdAt": "2026-10-02", "dismissedAt": None, "appUser": {"id": APP_ID}}]
        self.assertEqual(lin.ensure_session("ENG-1"), "live")
        self.assertEqual(order, [])  # no new session, nothing to announce

    def test_announcing_a_session_is_best_effort(self):
        class Down:
            def enabled(self):
                return True

            def expect_session(self, issue_id):
                raise apl.GatewayDown("network error: gateway unreachable")

        class Off:
            def enabled(self):
                return False

            def expect_session(self, issue_id):
                raise AssertionError("not set up: never called")

        apl.expect_own_session(Down())("uuid-1")  # warns, doesn't raise
        apl.expect_own_session(Off())("uuid-1")

    def disabled_sessions_client(self, posted):
        def gql(q, v):
            if "viewer" in q:
                return {"viewer": {"id": APP_ID, "name": "Autopilot", "displayName": "a", "app": True}}
            if "query Sessions" in q:
                return {"issue": {"id": "uuid-1", "agentSessions": {"nodes": []}}}
            if "commentCreate" in q:
                posted.append(v["input"]["body"])
                return {"commentCreate": {"success": True, "comment": {"id": "c1", "url": "u", "user": {"name": "Autopilot", "app": True}}}}
            raise AssertionError(q)

        def post(url, body, headers):
            if url == apl.API_URL and b"agentSessionCreateOnIssue" in body:
                return 200, {"errors": [{"message": "agent sessions disabled"}]}
            return FakeHTTP(graphql=gql)(url, body, headers)

        return apl.Linear(keychain=creds(), post=post, now=lambda: NOW, sleep=lambda s: None)

    def test_elicitation_falls_back_to_a_marked_comment_when_sessions_are_disabled(self):
        posted = []
        out = apl.run(["activity", "ENG-1", "--type", "elicitation", "--body", "Pick A or B?"],
                      client=self.disabled_sessions_client(posted))
        self.assertEqual(out["fallback"], "comment")
        self.assertEqual(posted, [apl.FALLBACK_PREFIX["elicitation"] + "Pick A or B?"])
        self.assertTrue(posted[0].startswith(apl.HOLD_MARKERS))

    def test_thought_and_link_are_skipped_when_sessions_are_disabled(self):
        posted = []
        c = self.disabled_sessions_client(posted)
        self.assertTrue(apl.run(["activity", "ENG-1", "--type", "thought", "--body", "x"], client=c)["skipped"])
        self.assertTrue(apl.run(["link", "ENG-1", "--label", "PR", "--url", "https://x"], client=c)["skipped"])
        self.assertEqual(posted, [])

    def test_error_fallback_is_marked(self):
        posted = []
        apl.run(["activity", "ENG-1", "--type", "error", "--body", "Stopped."], client=self.disabled_sessions_client(posted))
        self.assertEqual(posted, [apl.FALLBACK_PREFIX["error"] + "Stopped."])
        self.assertTrue(posted[0].startswith(apl.HOLD_MARKERS))

    def test_other_session_errors_are_not_swallowed(self):
        def post(url, body, headers):
            if url == apl.API_URL and b"agentSessionCreateOnIssue" in body:
                return 200, {"errors": [{"message": "Entity not found"}]}
            if url == apl.API_URL and b"query Sessions" in body:
                return 200, {"data": {"issue": {"id": "uuid-1", "agentSessions": {"nodes": []}}}}
            return FakeHTTP()(url, body, headers)

        c = apl.Linear(keychain=creds(), post=post, now=lambda: NOW, sleep=lambda s: None)
        with self.assertRaises(apl.LinearError):
            c.activity("ENG-1", {"type": "elicitation", "body": "x"})

    def test_response_fallback_is_an_unmarked_comment(self):
        posted = []
        apl.run(["activity", "ENG-1", "--type", "response", "--body", "Done."], client=self.disabled_sessions_client(posted))
        self.assertEqual(posted, ["Done."])
        self.assertFalse(posted[0].startswith(apl.HOLD_MARKERS))

    def test_action_activity_needs_action_and_parameter(self):
        with self.assertRaises(apl.LinearError):
            apl.run(["activity", "ENG-1", "--type", "action"], client=client(creds()))

    def test_only_thought_and_action_can_be_ephemeral(self):
        with self.assertRaises(apl.LinearError):
            apl.run(["activity", "ENG-1", "--type", "response", "--body", "x", "--ephemeral"], client=client(creds()))

    def test_pick_state_by_name_then_type(self):
        states = [
            {"id": "1", "name": "Todo", "type": "unstarted", "position": 1},
            {"id": "2", "name": "In Progress", "type": "started", "position": 2},
            {"id": "3", "name": "In Review", "type": "started", "position": 3},
            {"id": "4", "name": "Done", "type": "completed", "position": 4},
        ]
        self.assertEqual(apl.pick_state(states, "in review")["id"], "3")
        self.assertEqual(apl.pick_state(states, "started")["id"], "2")
        self.assertIsNone(apl.pick_state(states, "Shipped"))


def job(ident="ENG-200", status="queued", sid="sess-1", jid="11111111-2222-3333-4444-555555555555", attempts=0):
    return {"id": jid, "kind": "created", "status": status, "sessionId": sid, "issueId": f"uuid-{ident}",
            "identifier": ident, "title": f"t {ident}", "url": "u", "team": "ENG", "commentId": None,
            "prompt": None, "attempts": attempts, "createdAt": "2026-10-19T10:00:00.000Z"}


def issue_data(ident, project="Autopilot Sandbox", state="unstarted", children=(), comments=()):
    return {"identifier": ident, "title": f"t {ident}", "url": "u", "priority": 3, "createdAt": "2026-10-01",
            "state": {"name": state.title(), "type": state}, "team": {"key": "ENG"}, "project": {"name": project},
            "labels": [], "openChildren": list(children), "comments": list(comments)}


class FakeGateway:
    def __init__(self, jobs=(), pending=None):
        self.jobs = list(jobs)
        self.pending = len(self.jobs) if pending is None else pending
        self.updates = []

    def enabled(self):
        return True

    def next(self):
        return {"pending": self.pending, "job": self.jobs[0] if self.jobs else None, "jobs": self.jobs}

    def update(self, job_id, action, note=None):
        self.updates.append((job_id, action, note))
        j = dict(next(j for j in self.jobs if j["id"] == job_id))
        j["status"] = {"ack": "running"}.get(action, action)
        return j


class FakeIssues:
    """A Linear stand-in for gateway mode: issue() by uuid, activity() recorded."""

    def __init__(self, issues):
        self.issues = issues
        self.activities = []

    def issue(self, ident):
        return self.issues[ident]

    def activity(self, ident, content, ephemeral=False, session_id=None):
        self.activities.append((ident, content["type"], session_id))
        return {"sessionId": session_id}


class GatewayQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(apl, "STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_count_comes_from_the_gateway_without_calling_linear(self):
        lin = FakeIssues({})
        self.assertEqual(apl.run(["queue", "--count"], client=lin, gateway=FakeGateway([job()], pending=3)), 3)

    def test_queued_count_leaves_out_jobs_a_run_has_taken(self):
        lin = FakeIssues({})
        jobs = [job(status="running"), job("ENG-2", jid="bbbbbbbb-2222-3333-4444-555555555555")]
        self.assertEqual(apl.run(["queue", "--count"], client=lin, gateway=FakeGateway(jobs)), 2)
        held = jobs[0]["identifier"]
        with mock.patch.object(apl, "_held_issues", return_value={held}):  # its run is alive
            self.assertEqual(apl.run(["queue", "--count", "--queued"], client=lin, gateway=FakeGateway(jobs)), 1)

    def test_queued_count_takes_in_a_running_job_whose_run_died(self):
        """ENG-219: the run was cut off with its job "running"; no run would ever wake for it."""
        lin = FakeIssues({})
        jobs = [job(status="running")]
        with mock.patch.object(apl, "_held_issues", return_value=set()):  # no live run holds it
            self.assertEqual(apl.run(["queue", "--count", "--queued"], client=lin, gateway=FakeGateway(jobs)), 1)
        with mock.patch.object(apl, "_held_issues", side_effect=AssertionError("not needed")):
            self.assertEqual(apl.run(["queue", "--count", "--queued"], client=lin, gateway=FakeGateway([job()])), 1)

    def test_next_is_the_first_job_with_its_issue_and_session(self):
        lin = FakeIssues({"uuid-ENG-200": issue_data("ENG-200", comments=[
            {"createdAt": "2026-10-19T09:00:00Z", "user": {"name": "Owner", "app": False}},
            {"createdAt": "2026-10-19T09:30:00Z", "user": {"name": "Autopilot", "app": True}},
        ])})
        out = apl.run(["queue", "--next"], client=lin, gateway=FakeGateway([job()]))
        self.assertEqual(out["identifier"], "ENG-200")
        self.assertEqual(out["project"], "Autopilot Sandbox")
        self.assertEqual(out["session"], {"id": "sess-1"})
        self.assertEqual(out["job"]["id"], job()["id"])
        self.assertEqual(out["lastHumanAt"], "2026-10-19T09:00:00Z")
        self.assertTrue(out["eligible"])
        self.assertFalse(out["resume"])

    def test_github_job_keeps_its_source_and_pr(self):
        lin = FakeIssues({"uuid-ENG-200": issue_data("ENG-200")})
        gh_job = {**job(), "kind": "prompted", "source": "github", "prompt": "On GitHub, @o commented on PR o/r#7 (u):\n\nfix it",
                  "github": {"repo": "o/r", "pr": 7, "commentUrl": "https://github.com/o/r/pull/7#issuecomment-1"}}
        out = apl.run(["queue", "--next"], client=lin, gateway=FakeGateway([gh_job]))
        self.assertEqual(out["job"]["source"], "github")
        self.assertEqual(out["job"]["github"], gh_job["github"])
        self.assertTrue(out["job"]["prompt"].endswith("fix it"))

    def test_running_job_is_a_resume_and_open_children_hold_it(self):
        lin = FakeIssues({"uuid-ENG-200": issue_data("ENG-200", children=["ENG-201"])})
        out = apl.run(["queue", "--next"], client=lin, gateway=FakeGateway([job(status="running")]))
        self.assertTrue(out["resume"])
        self.assertFalse(out["eligible"])
        self.assertEqual(out["heldBecause"], "open sub-issues: ENG-201")

    def test_excluded_project_is_passed_over_not_stalled_on(self):
        a = job("ENG-1", jid="aaaaaaaa-2222-3333-4444-555555555555")
        b = job("ENG-2", jid="bbbbbbbb-2222-3333-4444-555555555555")
        lin = FakeIssues({"uuid-ENG-1": issue_data("ENG-1", project="Other"), "uuid-ENG-2": issue_data("ENG-2")})
        out = apl.run(["queue", "--next", "--exclude-project", "Other"], client=lin, gateway=FakeGateway([a, b]))
        self.assertEqual(out["identifier"], "ENG-2")
        self.assertIsNone(apl.run(["queue", "--next"], client=FakeIssues({}), gateway=FakeGateway([])))

    def test_list_resolves_every_job_in_order_and_keeps_the_project_skip(self):
        a = job("ENG-1", jid="aaaaaaaa-2222-3333-4444-555555555555")
        b = job("ENG-2", jid="bbbbbbbb-2222-3333-4444-555555555555")
        c = job("ENG-3", jid="cccccccc-2222-3333-4444-555555555555")
        lin = FakeIssues({"uuid-ENG-1": issue_data("ENG-1"), "uuid-ENG-2": issue_data("ENG-2", project="Other"),
                          "uuid-ENG-3": issue_data("ENG-3")})
        out = apl.run(["queue", "--list", "--exclude-project", "Other"], client=lin, gateway=FakeGateway([a, b, c]))
        self.assertEqual([i["identifier"] for i in out["items"]], ["ENG-1", "ENG-3"])
        self.assertEqual(out["items"][0]["job"]["id"], a["id"])

    def test_a_second_session_of_the_issue_does_not_take_over_the_run(self):
        a = job()
        b = job(sid="sess-2", jid="bbbbbbbb-2222-3333-4444-555555555555")
        gw, lin = FakeGateway([a, b]), FakeIssues({})
        apl.run(["jobs", "ack", a["id"]], client=lin, gateway=gw)
        apl.run(["jobs", "ack", b["id"]], client=lin, gateway=gw)
        self.assertEqual(lin.activities, [("ENG-200", "thought", "sess-2")])  # the pointer in the new one
        lin.activities.clear()
        apl.run(["activity", "ENG-200", "--type", "thought", "--body", "working"], client=lin, gateway=gw)
        out = apl.run(["activity", "ENG-200", "--type", "response", "--body", "done"], client=lin, gateway=gw)
        self.assertEqual(lin.activities, [("ENG-200", "thought", "sess-1"),
                                          ("ENG-200", "response", "sess-1"), ("ENG-200", "response", "sess-2")])
        self.assertEqual(out["mirroredTo"], ["sess-2"])
        apl.run(["jobs", "done", b["id"]], client=lin, gateway=gw)
        lin.activities.clear()
        apl.run(["activity", "ENG-200", "--type", "error", "--body", "x"], client=lin, gateway=gw)
        self.assertEqual(lin.activities, [("ENG-200", "error", "sess-1")])  # b is finished: no copy
        apl.run(["jobs", "done", a["id"]], client=lin, gateway=gw)
        self.assertIsNone(apl.read_current_job("ENG-200"))

    def test_a_note_in_the_runs_own_session_gets_no_pointer_and_a_failed_pointer_keeps_the_ack(self):
        a = job()
        note = job(sid="sess-1", jid="cccccccc-2222-3333-4444-555555555555")
        other = job(sid="sess-3", jid="dddddddd-2222-3333-4444-555555555555")

        class Flaky(FakeIssues):
            def activity(self, ident, content, ephemeral=False, session_id=None):
                if session_id == "sess-3":
                    raise apl.LinearError("session closed")
                return super().activity(ident, content, ephemeral, session_id)

        gw, lin = FakeGateway([a, note, other]), Flaky({})
        apl.run(["jobs", "ack", a["id"]], client=lin, gateway=gw)
        apl.run(["jobs", "ack", note["id"]], client=lin, gateway=gw)
        self.assertEqual(lin.activities, [])  # same session: no "progress is posted there"
        out = apl.run(["jobs", "ack", other["id"]], client=lin, gateway=gw)  # pointer fails
        self.assertEqual(out["status"], "running")
        self.assertEqual(apl.read_current_job("ENG-200")["id"], a["id"])

    def test_source_linear_still_polls_linear(self):
        class Polling(FakeIssues):
            def queue(self, *a):
                return {"count": 7, "issues": [], "held": []}
        self.assertEqual(apl.run(["queue", "--count", "--source", "linear"], client=Polling({}), gateway=FakeGateway([job()])), 7)

    def test_ack_remembers_the_job_and_activities_go_to_its_session(self):
        gw, lin = FakeGateway([job()]), FakeIssues({})
        apl.run(["jobs", "ack", job()["id"]], client=lin, gateway=gw)
        apl.run(["activity", "eng-200", "--type", "thought", "--body", "hi"], client=lin, gateway=gw)
        apl.run(["activity", "ENG-999", "--type", "thought", "--body", "hi"], client=lin, gateway=gw)
        apl.run(["activity", "ENG-200", "--type", "thought", "--body", "hi", "--session", "explicit"], client=lin, gateway=gw)
        self.assertEqual([a[2] for a in lin.activities], ["sess-1", None, "explicit"])
        apl.run(["jobs", "done", job()["id"], "--note", "PR merged"], client=lin, gateway=gw)
        self.assertEqual(gw.updates[-1], (job()["id"], "done", "PR merged"))
        self.assertIsNone(apl.read_current_job("ENG-200"))
        apl.run(["activity", "ENG-200", "--type", "thought", "--body", "hi"], client=lin, gateway=gw)
        self.assertIsNone(lin.activities[-1][2])

    def test_jobs_needs_an_id(self):
        with self.assertRaises(apl.LinearError):
            apl.run(["jobs", "ack"], client=FakeIssues({}), gateway=FakeGateway())


class GatewayClientTests(unittest.TestCase):
    URL = "https://autopilot-gateway.example.workers.dev"

    def make(self, replies, items=None):
        calls = []

        def request(method, url, body, headers):
            calls.append((method, url, body, headers))
            return replies.pop(0)

        kc = FakeKeychain({"url": self.URL, "token": "gw-secret", **(items or {})})
        return apl.Gateway(keychain=kc, request=request, sleep=lambda s: None), calls

    def test_sends_the_token_only_as_a_bearer_header(self):
        gw, calls = self.make([(200, {"pending": 0, "job": None, "jobs": []})])
        self.assertEqual(gw.next()["pending"], 0)
        method, url, body, headers = calls[0]
        self.assertEqual((method, url, body), ("GET", self.URL + "/jobs/next", None))
        self.assertEqual(headers["Authorization"], "Bearer gw-secret")
        # Not Python's default agent: Cloudflare's bot check rejects that with a 403.
        self.assertTrue(headers["User-Agent"].startswith("autopilot-runner/"))

    def test_posts_ack_and_notes(self):
        jid = job()["id"]
        gw, calls = self.make([(200, {"ok": True, "job": job(status="blocked")})])
        gw.update(jid, "blocked", "needs an answer")
        self.assertEqual(calls[0][1], f"{self.URL}/jobs/{jid}/blocked")
        self.assertEqual(json.loads(calls[0][2]), {"note": "needs an answer"})

    def test_announces_a_session_by_issue_id_once_and_briefly(self):
        calls = []

        def request(method, url, body, headers, timeout=30):
            calls.append((method, url, json.loads(body), timeout))
            return 503, None

        gw = apl.Gateway(keychain=FakeKeychain({"url": self.URL, "token": "gw-secret"}), request=request,
                         sleep=lambda s: self.fail("no retry inside a hook"))
        with self.assertRaises(apl.GatewayDown):
            gw.expect_session("uuid-1")
        self.assertEqual(calls, [("POST", self.URL + "/sessions/expect", {"issueId": "uuid-1"}, apl.EXPECT_TIMEOUT_S)])

    def test_get_retries_5xx_but_post_does_not(self):
        gw, calls = self.make([(503, None), (0, {"networkError": "x"}), (200, {"pending": 1, "job": None, "jobs": []})])
        self.assertEqual(gw.next()["pending"], 1)
        gw, calls = self.make([(503, None)])
        with self.assertRaises(apl.LinearError):
            gw.update(job()["id"], "ack")
        self.assertEqual(len(calls), 1)

    def test_errors_never_echo_the_token(self):
        gw, _ = self.make([(401, {"error": "unauthorized"})])
        with self.assertRaises(apl.AuthError) as cm:
            gw.next()
        self.assertNotIn("gw-secret", str(cm.exception))
        gw, _ = self.make([(409, {"error": "job is done"})])
        with self.assertRaises(apl.LinearError) as cm:
            gw.update(job()["id"], "ack")
        self.assertIn("job is done", str(cm.exception))

    def test_setup_checks(self):
        with mock.patch.dict(os.environ, {"AUTOPILOT_GATEWAY": ""}):
            self.assertFalse(apl.Gateway(keychain=FakeKeychain()).enabled())
            self.assertTrue(apl.Gateway(keychain=FakeKeychain({"url": self.URL})).enabled())
        self.assertFalse(apl.Gateway(keychain=FakeKeychain({"url": self.URL})).enabled())  # AUTOPILOT_GATEWAY=off
        with self.assertRaises(apl.SetupError):
            apl.Gateway(keychain=FakeKeychain({"url": self.URL})).next()  # no token
        with self.assertRaises(apl.SetupError):
            apl.Gateway(keychain=FakeKeychain({"url": "http://evil.example", "token": "t"})).next()
        with self.assertRaises(apl.LinearError):
            self.make([])[0].update("../../x", "ack")


class GatewayReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(apl, "STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_missing_issue_is_blocked_and_the_next_job_taken(self):
        a = job("ENG-1", jid="aaaaaaaa-2222-3333-4444-555555555555")
        b = job("ENG-2", jid="bbbbbbbb-2222-3333-4444-555555555555")

        class Lin(FakeIssues):
            def issue(self, ident):
                if ident == "uuid-ENG-1":
                    raise apl.NotFoundError("issue uuid-ENG-1 not found")
                return super().issue(ident)

        gw = FakeGateway([a, b])
        out = apl.run(["queue", "--next"], client=Lin({"uuid-ENG-2": issue_data("ENG-2")}), gateway=gw)
        self.assertEqual(out["identifier"], "ENG-2")
        self.assertEqual(gw.updates, [(a["id"], "blocked", "issue not found")])

    def test_gateway_outage_falls_back_to_linear_polling_in_auto_mode_only(self):
        class Down(FakeGateway):
            def next(self):
                raise apl.GatewayDown("gateway unreachable")

        class Polling(FakeIssues):
            def queue(self, *a):
                return {"count": 2, "issues": [], "held": []}

        self.assertEqual(apl.run(["queue", "--count"], client=Polling({}), gateway=Down()), 2)
        with self.assertRaises(apl.GatewayDown):
            apl.run(["queue", "--count", "--source", "gateway"], client=Polling({}), gateway=Down())

    def test_a_401_does_not_fall_back(self):
        class Rejected(FakeGateway):
            def next(self):
                raise apl.AuthError("rejected")
        with self.assertRaises(apl.AuthError):
            apl.run(["queue", "--count"], client=FakeIssues({}), gateway=Rejected())

    def test_current_job_older_than_six_hours_is_ignored(self):
        apl.write_current_job(job())
        self.assertEqual(apl.read_current_job("ENG-200")["sessionId"], "sess-1")
        old = apl.time.time() - 7 * 3600
        os.utime(apl.current_job_path("ENG-200"), (old, old))
        self.assertIsNone(apl.read_current_job("ENG-200"))

    def test_parallel_runs_keep_their_issues_apart(self):
        a = job("ENG-1", sid="s-1", jid="aaaaaaaa-2222-3333-4444-555555555555")
        b = job("ENG-2", sid="s-2", jid="bbbbbbbb-2222-3333-4444-555555555555")
        gw, lin = FakeGateway([a, b]), FakeIssues({})
        apl.run(["jobs", "ack", a["id"]], client=lin, gateway=gw)
        apl.run(["jobs", "ack", b["id"]], client=lin, gateway=gw)  # another run, another issue
        self.assertEqual(lin.activities, [])  # no pointer: different issues
        apl.run(["activity", "ENG-1", "--type", "thought", "--body", "x"], client=lin, gateway=gw)
        apl.run(["activity", "ENG-2", "--type", "thought", "--body", "y"], client=lin, gateway=gw)
        self.assertEqual([x[2] for x in lin.activities], ["s-1", "s-2"])
        apl.run(["jobs", "done", a["id"]], client=lin, gateway=gw)
        self.assertIsNone(apl.read_current_job("ENG-1"))
        self.assertEqual(apl.read_current_job("ENG-2")["id"], b["id"])

    def test_failed_done_still_forgets_the_job(self):
        class Conflict(FakeGateway):
            def update(self, job_id, action, note=None):
                if action == "ack":
                    return super().update(job_id, action, note)
                raise apl.LinearError("gateway POST failed (HTTP 409): job is cancelled")
        gw = Conflict([job()])
        apl.run(["jobs", "ack", job()["id"]], client=FakeIssues({}), gateway=gw)
        with self.assertRaises(apl.LinearError):
            apl.run(["jobs", "blocked", job()["id"]], client=FakeIssues({}), gateway=gw)
        self.assertFalse(os.path.exists(apl.current_job_path("ENG-200")))

    def test_env_url_override_is_localhost_only(self):
        kc = FakeKeychain({"url": "https://gw.example.workers.dev", "token": "t"})
        with mock.patch.dict(os.environ, {"AUTOPILOT_GATEWAY_URL": "https://evil.example"}):
            with self.assertRaises(apl.SetupError):
                apl.Gateway(keychain=kc, request=lambda *a: (200, {})).next()
        calls = []
        with mock.patch.dict(os.environ, {"AUTOPILOT_GATEWAY_URL": "http://localhost:8787"}):
            apl.Gateway(keychain=kc, request=lambda *a: calls.append(a) or (200, {"pending": 0})).next()
        self.assertEqual(calls[0][1], "http://localhost:8787/jobs/next")
        with self.assertRaises(apl.SetupError):  # a stored http:// URL is refused
            apl.Gateway(keychain=FakeKeychain({"url": "http://localhost:8787", "token": "t"})).next()

    def test_done_is_retried_but_ack_is_not(self):
        replies = [(502, None), (200, {"ok": True, "job": job(status="done")})]
        calls = []
        kc = FakeKeychain({"url": "https://gw.example.workers.dev", "token": "t"})
        gw = apl.Gateway(keychain=kc, request=lambda *a: calls.append(a) or replies.pop(0), sleep=lambda s: None)
        self.assertEqual(gw.update(job()["id"], "done")["status"], "done")
        self.assertEqual(len(calls), 2)

    def test_jobs_get_reads_one_job(self):
        kc = FakeKeychain({"url": "https://gw.example.workers.dev", "token": "t"})
        gw = apl.Gateway(keychain=kc, request=lambda *a: (200, {"job": {**job(status="running"), "stopRequested": True}}))
        self.assertTrue(apl.run(["jobs", "get", job()["id"]], client=FakeIssues({}), gateway=gw)["stopRequested"])

    def test_redirects_are_not_followed(self):
        handler = apl._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/"))


if __name__ == "__main__":
    unittest.main()
