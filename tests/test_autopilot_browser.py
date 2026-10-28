"""Unit tests for bin/autopilot-browser: the browser host. Stdlib only.

A fake Chromium (a Python script serving /json/version and starting a fake native host) stands in
for the real one, so start, status and stop run end to end without a browser.
"""

import argparse
import contextlib
import io
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

FAKE_CHROMIUM = textwrap.dedent("""\
    import http.server, json, subprocess, sys
    port = int(next(a for a in sys.argv if a.startswith('--remote-debugging-port=')).split('=')[1])
    # A native host child, the way a browser starts one for an extension.
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', ' chrome-extension://abc/'])
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({'Browser': 'Fake/1'}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *a): pass
    http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()
""")

FAKE_PASSHAND = textwrap.dedent("""\
    import json, sys
    for line in sys.stdin:
        m = json.loads(line)
        if 'id' not in m: continue
        if m['method'] == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {}}
        elif m['params']['name'] == 'list_saved_logins':
            r = {'content': [{'type': 'text', 'text':
                 'https://staging.example.com  \\u2014  test-user  \\u2014  auto\\n'
                 'https://other.example.com  \\u2014  someone  \\u2014  touch id'}]}
        else:
            a = m['params']['arguments']
            r = {'content': [{'type': 'text', 'text': 'Filled the username and password for %s on %s. '
                 'Now click the sign-in button.' % (a['username'], a['page_url'])}], 'isError': False}
        print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': r}), flush=True)
""")


def _load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["AUTOPILOT_STATE_DIR"] = self.tmp.name
        self.ab = _load("autopilot-browser")

    def tearDown(self):
        del os.environ["AUTOPILOT_STATE_DIR"]
        self.tmp.cleanup()


class HostTests(Base):
    def test_allowed_hosts_are_the_web_host_api_hosts_and_extras_once_each(self):
        hosts = self.ab.allowed_hosts("https://Staging.example.com:8443/app", ["api.staging.example.com"],
                                      ["fonts.example.net", "staging.example.com"], ["example.com"])
        self.assertEqual(hosts, ["staging.example.com", "api.staging.example.com", "fonts.example.net"])

    def test_a_forbidden_host_can_never_be_allowed(self):
        with self.assertRaises(self.ab.Fail) as e:
            self.ab.allowed_hosts("https://staging.example.com", ["api.example.com"], [], ["API.example.com"])
        self.assertEqual(e.exception.code, 2)

    def test_a_wildcard_may_not_cover_a_forbidden_host(self):
        with self.assertRaises(self.ab.Fail):
            self.ab.allowed_hosts("https://staging.example.com", [], ["*.example.com"], ["api.example.com"])
        self.assertEqual(self.ab.allowed_hosts("https://staging.example.com", [], ["*.cdn.example.net"],
                                               ["api.example.com"]), ["staging.example.com", "*.cdn.example.net"])

    def test_a_forbidden_wildcard_covers_an_allowed_host_too(self):
        with self.assertRaises(self.ab.Fail):
            self.ab.allowed_hosts("https://staging.example.com", ["api.example.com"], [], ["*.example.com"])

    def test_loopback_is_never_allowed(self):
        for h in ("localhost", "app.localhost", "127.0.0.1", "0.0.0.0"):
            with self.subTest(host=h), self.assertRaises(self.ab.Fail) as e:
                self.ab.allowed_hosts("https://staging.example.com", [], [h])
            self.assertEqual(e.exception.code, 2)

    def test_bad_host_names_are_refused(self):
        for bad in ("", "exa mple.com", "staging.example.com,evil.com", "-x.example.com"):
            with self.subTest(bad=bad), self.assertRaises(self.ab.Fail):
                self.ab.host_of(bad)

    def test_resolver_rules_block_everything_but_the_allowed_hosts(self):
        self.assertEqual(self.ab.resolver_rules(["a.example.com", "b.example.com"]),
                         "MAP * ~NOTFOUND, EXCLUDE a.example.com, EXCLUDE b.example.com")

    def test_chromium_args(self):
        args = self.ab.chromium_args("/p", 9333, ["a.example.com"])
        self.assertIn("--user-data-dir=/p", args)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertIn("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE a.example.com", args)
        self.assertFalse([a for a in args if "extension" in a])
        self.assertIn("--no-proxy-server", args)
        args = self.ab.chromium_args("/p", 9333, ["a.example.com"], ["/ext"])
        self.assertIn("--load-extension=/ext", args)
        self.assertIn("--disable-extensions-except=/ext", args)

    def test_playwright_mcp_args_use_the_same_hosts(self):
        self.assertEqual(self.ab.playwright_mcp_args(9333, ["a.example.com", "b.example.com"]),
                         ["--cdp-endpoint", "http://127.0.0.1:9333",
                          "--allowed-origins", "https://a.example.com;https://b.example.com"])


class ProcessTests(Base):
    PROCS = {
        1: (0, "/sbin/launchd"),
        10: (1, "/x/Chromium --user-data-dir=/p --remote-debugging-port=9333 about:blank"),
        11: (10, "/x/Chromium Helper (Renderer) --type=renderer --user-data-dir=/p"),
        12: (10, "/x/chrome_crashpad_handler --database=/p/Crashpad"),
        13: (10, "/x/passhand chrome-extension://abc/"),
        14: (11, "/x/grandchild"),
        20: (1, "/Applications/Google Chrome --flag"),
        21: (20, "/x/passhand chrome-extension://abc/"),
        30: (1, "/x/Chromium --user-data-dir=/other"),
    }

    def test_finds_the_main_process_of_this_profile_only(self):
        self.assertEqual(self.ab.find_main(self.PROCS, "/p", "/x/Chromium"), 10)
        self.assertIsNone(self.ab.find_main(self.PROCS, "/missing", "/x/Chromium"))
        self.assertIsNone(self.ab.find_main(self.PROCS, "/oth", "/x/Chromium"))  # a prefix of /other is another profile
        procs = {40: (1, "/x/Chromium --user-data-dir=/p2 about:blank"),
                 41: (1, "pgrep -f --user-data-dir=/p"), 42: (1, "/other/Chromium --user-data-dir=/p")}
        self.assertIsNone(self.ab.find_main(procs, "/p", "/x/Chromium"))  # only our binary on this profile
        self.assertIsNone(self.ab.find_main(self.PROCS, "/p", ""))  # start never ran

    def test_descendants_and_native_hosts(self):
        family = self.ab.descendants(self.PROCS, 10)
        self.assertEqual(family, [11, 12, 13, 14])
        self.assertEqual(self.ab.native_hosts(self.PROCS, family), [13])  # not the owner's Chrome's (21)


class WebSocketTests(Base):
    def test_frames_round_trip_at_every_length_encoding(self):
        for n in (0, 5, 125, 126, 65535, 65536):
            payload = bytes(i % 251 for i in range(n))
            frame = self.ab.ws_frame(payload, mask=b"\x01\x02\x03\x04")
            fin, opcode, got, rest = self.ab.ws_parse(frame + b"next")
            self.assertEqual((fin, opcode, got, rest), (True, 1, payload, b"next"))
            self.assertIsNone(self.ab.ws_parse(frame[:-1]))

    def test_cdp_answers_its_own_call_skipping_events_and_pings(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        got = {}

        def serve():
            conn, _ = server.accept()
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(4096)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            rest = buf.split(b"\r\n\r\n", 1)[1]
            while not self.ab.ws_parse(rest):
                rest += conn.recv(65536)
            got["request"] = json.loads(self.ab.ws_parse(rest)[2])

            def unmasked(data, opcode=1):
                return bytes([0x80 | opcode, len(data)]) + data

            conn.sendall(unmasked(json.dumps({"method": "Page.loadEventFired"}).encode())
                         + unmasked(b"hi", opcode=0x9)
                         + unmasked(json.dumps({"id": 1, "result": {"ok": True}}).encode()))
            time.sleep(0.2)
            conn.close()

        t = threading.Thread(target=serve)
        t.start()
        cdp = self.ab.Cdp(f"ws://127.0.0.1:{port}/devtools/page/X", timeout=5)
        self.assertEqual(cdp.send("Runtime.evaluate", {"expression": "1"}), {"ok": True})
        cdp.close()
        t.join()
        server.close()
        self.assertEqual(got["request"]["method"], "Runtime.evaluate")

    def test_cdp_refuses_a_remote_endpoint(self):
        with self.assertRaises(self.ab.Fail):
            self.ab.Cdp("ws://192.0.2.1:9333/devtools/browser/x")


class PasshandTests(Base):
    def test_lists_logins_with_their_fill_mode_and_fills(self):
        script = pathlib.Path(self.tmp.name, "passhand")
        script.write_text(f"#!{sys.executable}\nimport sys\nsys.argv.pop(1)\n" + FAKE_PASSHAND)
        script.chmod(0o755)
        ph = self.ab.Passhand(str(script), timeout=10)
        try:
            self.assertEqual(ph.saved_logins(), [("https://staging.example.com", "test-user", "auto"),
                                                 ("https://other.example.com", "someone", "touch id")])
            text, is_error = ph.fill("https://staging.example.com/login", "test-user")
            self.assertFalse(is_error)
            self.assertTrue(text.startswith("Filled the username and password for test-user"))
        finally:
            ph.close()


class FakePage:
    """The page as sign_in sees it: a login form until the sign-in button is clicked."""

    def __init__(self, signed_in=False, form=True, two_step=False, sign_in_works=True, slow=0):
        self.signed_in, self.form, self.two_step, self.works = signed_in, form, two_step, sign_in_works
        self.clicks, self.closed, self.url = [], False, None
        self.step = 1
        self.slow, self.pending = slow, None  # probes the form stays up after Sign in, like a slow server

    def navigate(self, url):
        self.url = url
        self.navigations = getattr(self, "navigations", 0) + 1

    def href(self):
        return "https://staging.example.com/login"

    def probe(self, user):
        if self.pending is not None:
            self.pending -= 1
            if self.pending <= 0:
                self.signed_in, self.pending = True, None
        if self.signed_in:
            return {"ready": True, "password": False, "username_field": False, "user": True}
        if not self.form:
            return {"ready": True, "password": False, "username_field": False, "user": False}
        password = not self.two_step or self.step == 2
        return {"ready": True, "password": password, "username_field": True, "user": False}

    def has_text(self, text):
        return text == "Maybe Later" and "Maybe Later" not in self.clicks

    def click_text(self, text, before=None):
        if before:
            before()
        self.clicks.append(text)
        if text == "Next":
            self.step = 2
        if text == "Sign in" and self.works:
            if self.slow:
                self.pending = self.slow
            else:
                self.signed_in = True
        return True

    def tick(self, label):
        self.clicks.append(f"tick:{label}")
        return "ticked"

    def message(self):
        return "Wrong username or password"

    def close(self):
        self.closed = True


class FakePasshand:
    def __init__(self, mode="auto", fill_text=None):
        self.mode, self.fill_text, self.fills = mode, fill_text, []

    def saved_logins(self):
        return [("https://staging.example.com", "test-user", self.mode)]

    def fill(self, page_url, username):
        self.fills.append((page_url, username))
        if self.fill_text:
            return self.fill_text, True
        if len(self.fills) == 1 and getattr(self, "two_step", False):
            return "Filled only the username for test-user on https://staging.example.com — click Next.", False
        return "Filled the username and password for test-user on https://staging.example.com.", False


class SignInTests(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(self.ab.HOST_DIR)
        self.ab.write_host({"project": "web-app", "port": free_port(), "allow_hosts": ["staging.example.com"],
                            "forbidden_hosts": ["example.com"]})
        self.failed = pathlib.Path(self.tmp.name, "web-app.browser-host.signin-failed")
        self.chrome_gate = pathlib.Path(self.tmp.name, "web-app.signin-failed")  # orchestrator 1c's, for Chrome

    def args(self, **kw):
        base = dict(url="https://staging.example.com/settings", user="test-user", username=None, passhand=True,
                    dismiss=["Maybe Later"], button="Sign in", next_button="Next", keep_signed_in=None,
                    project=None, timeout=1)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_already_signed_in_clears_an_old_failure(self):
        self.failed.write_text("x")
        page = FakePage(signed_in=True)
        out, code = self.ab.sign_in(self.args(), page=page, passhand=FakePasshand())
        self.assertEqual((out["state"], code), ("signed-in", 0))
        self.assertFalse(self.failed.exists())
        self.assertTrue(page.closed)

    def test_signs_in_with_passhand_after_dismissing_the_first_run_prompt(self):
        page, ph = FakePage(), FakePasshand()
        out, code = self.ab.sign_in(self.args(keep_signed_in="Keep me logged in"), page=page, passhand=ph)
        self.assertEqual((out["state"], code), ("signed-in", 0))
        self.assertEqual(page.clicks, ["Maybe Later", "tick:Keep me logged in", "Sign in"])
        self.assertEqual(ph.fills, [("https://staging.example.com/login", "test-user")])
        self.assertFalse(self.failed.exists())

    def test_a_slow_sign_in_is_waited_for_not_failed(self):
        page = FakePage(slow=4)  # the form stays up ~2 s after the click
        out, code = self.ab.sign_in(self.args(timeout=4), page=page, passhand=FakePasshand())
        self.assertEqual((out["state"], code), ("signed-in", 0))
        self.assertEqual(page.navigations, 1)  # never reloaded under a sign-in on its way
        self.assertFalse(self.failed.exists())

    def test_a_passhand_error_during_the_fill_is_not_an_attempt(self):
        class Hanging(FakePasshand):
            def fill(self, page_url, username):
                raise self_ab.Fail("Passhand tools/call: no answer in 150 s")
        self_ab = self.ab
        out, code = self.ab.sign_in(self.args(), page=FakePage(), passhand=Hanging())
        self.assertEqual((out["state"], code), ("not-tried", 3))
        self.assertFalse(self.failed.exists())  # fill_login never submits: nothing reached the server

    def test_not_running_comes_before_the_allowlist(self):
        os.remove(self.ab.HOST_FILE)
        self.ab.find_main = lambda procs, profile=None: None
        out, code = self.ab.sign_in(self.args())
        self.assertEqual((out["state"], code), ("not-running", 3))

    def test_two_step_login_clicks_next_and_fills_again(self):
        page, ph = FakePage(two_step=True), FakePasshand()
        ph.two_step = True
        out, code = self.ab.sign_in(self.args(), page=page, passhand=ph)
        self.assertEqual(out["state"], "signed-in")
        self.assertEqual(len(ph.fills), 2)
        self.assertIn("Next", page.clicks)

    def test_without_passhand_the_login_page_is_left_open_for_the_owner(self):
        page = FakePage()
        out, code = self.ab.sign_in(self.args(passhand=False), page=page, passhand=FakePasshand())
        self.assertEqual((out["state"], code), ("signed-out", 3))
        self.assertFalse(page.closed)
        self.assertFalse(self.failed.exists())

    def test_a_login_that_isnt_auto_is_not_tried(self):
        ph = FakePasshand(mode="touch id")
        out, code = self.ab.sign_in(self.args(), page=FakePage(), passhand=ph)
        self.assertEqual((out["state"], code), ("not-tried", 3))
        self.assertEqual(ph.fills, [])
        self.assertFalse(self.failed.exists())

    def test_a_login_without_a_fill_mode_is_not_tried(self):
        out, _ = self.ab.sign_in(self.args(), page=FakePage(), passhand=FakePasshand(mode=None))
        self.assertEqual(out["state"], "not-tried")

    def test_passhand_needs_a_project_for_the_gate(self):
        self.ab.write_host({"port": free_port(), "allow_hosts": ["staging.example.com"]})
        ph = FakePasshand()
        out, _ = self.ab.sign_in(self.args(), page=FakePage(), passhand=ph)
        self.assertEqual(out["state"], "not-tried")
        self.assertEqual(ph.fills, [])

    def test_an_attempt_that_crashes_after_the_click_still_counts(self):
        class Crashing(FakePage):
            def probe(self, user):
                if "Sign in" in self.clicks:
                    raise OSError("CDP went away")
                return super().probe(user)
        with self.assertRaises(OSError):
            self.ab.sign_in(self.args(), page=Crashing(), passhand=FakePasshand())
        self.assertTrue(self.failed.exists())

    def test_a_crash_before_the_click_is_not_an_attempt(self):
        class Crashing(FakePage):
            def tick(self, label):
                raise OSError("CDP went away")
        with self.assertRaises(OSError):
            self.ab.sign_in(self.args(keep_signed_in="Keep me"), page=Crashing(), passhand=FakePasshand())
        self.assertFalse(self.failed.exists())

    def test_chromes_gate_is_never_read_or_touched(self):
        self.chrome_gate.write_text("x")
        out, _ = self.ab.sign_in(self.args(), page=FakePage(), passhand=FakePasshand())
        self.assertEqual(out["state"], "signed-in")  # Chrome's recent failure doesn't hold this one up
        self.assertTrue(self.chrome_gate.exists())   # nor does this success clear Chrome's
        self.ab.sign_in(self.args(), page=FakePage(sign_in_works=False), passhand=FakePasshand())
        self.assertTrue(self.failed.exists())
        self.assertEqual(self.chrome_gate.read_text(), "x")

    def test_no_saved_login_for_the_username_is_not_tried(self):
        out, _ = self.ab.sign_in(self.args(username="someone-else"), page=FakePage(), passhand=FakePasshand())
        self.assertEqual(out["state"], "not-tried")
        self.assertIn("no saved login", out["reason"])

    def test_a_failed_sign_in_is_recorded_and_not_retried_for_20_hours(self):
        out, code = self.ab.sign_in(self.args(), page=FakePage(sign_in_works=False), passhand=FakePasshand())
        self.assertEqual((out["state"], code), ("failed", 3))
        self.assertEqual(out["reason"], "Wrong username or password")
        self.assertTrue(self.failed.exists())
        ph = FakePasshand()
        out, _ = self.ab.sign_in(self.args(), page=FakePage(), passhand=ph)
        self.assertEqual(out["state"], "not-tried")
        self.assertEqual(ph.fills, [])
        out, _ = self.ab.sign_in(self.args(), page=FakePage(), passhand=ph, now=time.time() + 21 * 3600)
        self.assertEqual(out["state"], "signed-in")
        self.assertFalse(self.failed.exists())

    def test_passhand_refusing_to_fill_is_not_an_attempt(self):
        ph = FakePasshand(fill_text="Nothing was filled: the page is open in 2 browsers.")
        page = FakePage()
        out, code = self.ab.sign_in(self.args(), page=page, passhand=ph)
        self.assertEqual((out, code), ({"state": "not-tried", "reason": "Nothing was filled: the page is open in 2 browsers."}, 3))
        self.assertNotIn("Sign in", page.clicks)
        self.assertFalse(self.failed.exists())  # the next keep-alive may try again

    def test_neither_form_nor_user_is_unknown_and_never_signs_out(self):
        page = FakePage(form=False)
        out, code = self.ab.sign_in(self.args(), page=page, passhand=FakePasshand())
        self.assertEqual((out["state"], code), ("unknown", 3))
        self.assertEqual(page.clicks, ["Maybe Later"])

    def test_only_allowed_hosts(self):
        for url in ("https://example.com/", "https://evil.example.net/login"):
            with self.subTest(url=url), self.assertRaises(self.ab.Fail) as e:
                self.ab.sign_in(self.args(url=url), page=FakePage(), passhand=FakePasshand())
            self.assertEqual(e.exception.code, 2)


class LifecycleTests(Base):
    """start, status and stop against the fake Chromium."""

    def setUp(self):
        super().setUp()
        self.chromium = pathlib.Path(self.tmp.name, "Chromium")
        self.chromium.write_text(f"#!{sys.executable}\n" + FAKE_CHROMIUM)
        self.chromium.chmod(0o755)
        self.port = free_port()

    def tearDown(self):
        main = self.ab.find_main(self.ab.ps())
        if main:
            self.ab.stop_browser()
        super().tearDown()

    def main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.ab.main(argv)

    def start(self, project="web-app", **kw):
        return self.main(["start", "--project", project, "--web-url", "https://staging.example.com",
                             "--api-host", "api.staging.example.com", "--forbidden-host", "example.com",
                             "--port", str(kw.get("port", self.port)), "--chromium", str(self.chromium)])

    def test_start_status_stop_leave_nothing_behind(self):
        self.assertEqual(self.start(), 0)
        procs = self.ab.ps()
        main = self.ab.find_main(procs)
        self.assertIsNotNone(main)
        st = self.ab.status()
        self.assertTrue(st["running"] and st["cdp"])
        self.assertEqual(st["browser"], "Fake/1")
        self.assertEqual(st["allow_hosts"], ["staging.example.com", "api.staging.example.com"])
        deadline = time.time() + 5
        while time.time() < deadline and not self.ab.status()["helpers"]:
            time.sleep(0.1)
        helpers = self.ab.status()["helpers"]
        self.assertEqual(len(helpers), 1)

        self.assertEqual(self.start(), 0)  # the same settings again: already running, nothing new
        self.assertEqual(self.ab.find_main(self.ab.ps()), main)
        self.assertEqual(self.start(project="other"), 1)  # one browser host per Mac

        out = self.ab.stop_browser()
        self.assertTrue(out["stopped"])
        self.assertEqual(out["left"], [])
        self.assertFalse(self.ab.alive(main))
        self.assertFalse(self.ab.alive(helpers[0]))
        self.assertIsNone(self.ab.find_main(self.ab.ps()))
        self.assertEqual(self.main(["status"]), 3)

    def test_start_refuses_a_port_another_browser_serves(self):
        other = subprocess.Popen([sys.executable, "-c", FAKE_CHROMIUM, f"--remote-debugging-port={self.port}"])
        try:
            deadline = time.time() + 5
            while time.time() < deadline and self.ab.cdp_version(self.port) is None:
                time.sleep(0.1)
            self.assertEqual(self.start(), 1)
            self.assertIsNone(self.ab.find_main(self.ab.ps()))
        finally:
            for pid in self.ab.descendants(self.ab.ps(), other.pid):
                os.kill(pid, 9)
            other.kill()
            other.wait()

    def test_start_refuses_a_forbidden_api_host(self):
        code = self.main(["start", "--project", "web-app", "--web-url", "https://staging.example.com",
                             "--api-host", "api.example.com", "--forbidden-host", "api.example.com",
                             "--chromium", str(self.chromium)])
        self.assertEqual(code, 2)
        self.assertIsNone(self.ab.find_main(self.ab.ps()))


if __name__ == "__main__":
    unittest.main()
