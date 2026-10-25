"""Unit tests for bin/autopilot-github. Stdlib only; no network, no Keychain.

The RS256 signature is checked against openssl when it is installed (it is on macOS and
GitHub's Ubuntu runners)."""

import base64
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader("autopilot_github", str(ROOT / "bin" / "autopilot-github"))
_spec = importlib.util.spec_from_loader("autopilot_github", _loader)
apg = importlib.util.module_from_spec(_spec)
_loader.exec_module(apg)

NOW = 1_800_000_000
OPENSSL = shutil.which("openssl")


def _genkey():
    """(PKCS#8 DER, PKCS#1 DER, public PEM path dir) from openssl, or None."""
    if not OPENSSL:
        return None
    d = tempfile.mkdtemp()
    pem = os.path.join(d, "k.pem")
    subprocess.run([OPENSSL, "genrsa", "-out", pem, "2048"], check=True, capture_output=True)
    pkcs8 = subprocess.run([OPENSSL, "pkcs8", "-topk8", "-nocrypt", "-in", pem, "-outform", "DER"], check=True, capture_output=True).stdout
    pkcs1 = subprocess.run([OPENSSL, "rsa", "-in", pem, "-outform", "DER"], check=True, capture_output=True).stdout
    return d, pem, pkcs8, pkcs1


KEY = _genkey()


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
    def __init__(self, token="ghs_minted123", installation_status=200):
        self.calls = []
        self.token = token
        self.installation_status = installation_status

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, headers))
        if url.endswith("/orgs/example-org/installation"):
            return self.installation_status, {"id": 777} if self.installation_status == 200 else {"message": "Not Found"}
        if url.endswith("/app/installations/777/access_tokens"):
            return 201, {"token": self.token, "expires_at": "2027-01-15T08:30:00Z"}
        if "/users/" in url:
            return 200, {"id": 424242, "login": "claude-autopilot[bot]"}
        if url.endswith("/installation/repositories?per_page=100"):
            return 200, {"repositories": [{"full_name": "example-org/b"}, {"full_name": "example-org/a"}]}
        return 404, {"message": "Not Found"}


def keychain(**over):
    items = {"app-id": "123456", "app-slug": "claude-autopilot", "owner": "example-org",
             "private-key": base64.b64encode(KEY[2]).decode() if KEY else "x"}
    items.update(over)
    return FakeKeychain({k: v for k, v in items.items() if v is not None})


@unittest.skipUnless(KEY, "openssl not installed")
class RS256Tests(unittest.TestCase):
    def _openssl_sign(self, data):
        return subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", KEY[1]], input=data, check=True, capture_output=True).stdout

    def test_signature_matches_openssl_for_pkcs8_and_pkcs1(self):
        msg = b"header.payload"
        want = self._openssl_sign(msg)  # PKCS#1 v1.5 is deterministic
        self.assertEqual(apg.rsa_sign(apg.rsa_key(KEY[2]), msg), want)
        self.assertEqual(apg.rsa_sign(apg.rsa_key(KEY[3]), msg), want)

    def test_jwt_has_github_claims_and_verifies(self):
        jwt = apg.app_jwt("123456", apg.rsa_key(KEY[2]), NOW)
        h, c, s = jwt.split(".")
        pad = lambda x: base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
        self.assertEqual(json.loads(pad(h)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(pad(c)), {"iat": NOW - 60, "exp": NOW + 540, "iss": "123456"})
        self.assertEqual(pad(s), self._openssl_sign(f"{h}.{c}".encode()))

    def test_pem_or_base64_der_in_the_keychain(self):
        pem = open(KEY[1]).read()
        self.assertEqual(apg.decode_private_key(pem)["n"], apg.rsa_key(KEY[3])["n"])
        self.assertEqual(apg.decode_private_key(base64.b64encode(KEY[2]).decode())["n"], apg.rsa_key(KEY[3])["n"])

    def test_garbage_key_is_a_setup_error_that_does_not_echo_it(self):
        with self.assertRaises(apg.SetupError) as cm:
            apg.decode_private_key("c2VjcmV0LXZhbHVl")  # base64 of "secret-value"
        self.assertNotIn("c2VjcmV0", str(cm.exception))
        self.assertNotIn("secret-value", str(cm.exception))


@unittest.skipUnless(KEY, "openssl not installed")
class TokenTests(unittest.TestCase):
    def test_mints_via_the_org_installation_and_caches_with_expiry(self):
        kc, http = keychain(), FakeHTTP()
        app = apg.GitHubApp(kc, http, now=lambda: NOW)
        self.assertEqual(app.token(), "ghs_minted123")
        self.assertEqual(app.token_source, "minted")
        self.assertEqual(kc.comments["installation-token"], f"exp={NOW + 1800}")  # GitHub's expires_at
        # JWT for the app endpoints, never the installation token
        self.assertTrue(http.calls[0][2]["Authorization"].startswith("Bearer ey"))
        self.assertTrue(http.calls[1][2]["Authorization"].startswith("Bearer ey"))
        self.assertEqual(http.calls[1][1], "https://api.github.com/app/installations/777/access_tokens")

    def test_accepts_the_long_jwt_style_installation_tokens(self):
        long = "ghs_" + "eyJhbGciOi." + "A-b_c" * 70 + ".sig-nat_ure"
        kc = keychain()
        self.assertEqual(apg.GitHubApp(kc, FakeHTTP(token=long), now=lambda: NOW).token(), long)
        self.assertEqual(kc.items["installation-token"], long)

    def test_uses_a_cached_token_with_time_left(self):
        kc = keychain(**{"installation-token": "ghs_cached"})
        kc.comments["installation-token"] = f"exp={NOW + 3000}"
        http = FakeHTTP()
        app = apg.GitHubApp(kc, http, now=lambda: NOW)
        self.assertEqual(app.token(), "ghs_cached")
        self.assertEqual(http.calls, [])

    def test_never_uses_a_cached_personal_token_or_one_about_to_expire(self):
        for token, exp in (("ghp_personal", NOW + 3000), ("github_pat_x", NOW + 3000), ("ghs_old", NOW + 300), ("ghs_nocomment", None)):
            kc = keychain(**{"installation-token": token})
            if exp:
                kc.comments["installation-token"] = f"exp={exp}"
            app = apg.GitHubApp(kc, FakeHTTP(), now=lambda: NOW)
            self.assertEqual(app.token(), "ghs_minted123", token)

    def test_refuses_an_answer_that_is_not_an_installation_token(self):
        app = apg.GitHubApp(keychain(), FakeHTTP(token="ghp_personal"), now=lambda: NOW)
        with self.assertRaises(apg.GitHubError):
            app.token()

    def test_not_installed_is_an_error_not_a_setup_gap(self):
        app = apg.GitHubApp(keychain(), FakeHTTP(installation_status=404), now=lambda: NOW)
        with self.assertRaisesRegex(apg.GitHubError, "installed"):
            app.token()

    def test_missing_keychain_items_are_setup_errors(self):
        for missing in ("app-id", "owner", "private-key"):
            with self.assertRaises(apg.SetupError, msg=missing):
                apg.GitHubApp(keychain(**{missing: None}), FakeHTTP(), now=lambda: NOW).token()

    def test_identity_is_the_bot_with_its_noreply_email(self):
        kc = keychain()
        ident = apg.GitHubApp(kc, FakeHTTP(), now=lambda: NOW).identity()
        self.assertEqual(ident, {"name": "claude-autopilot[bot]", "email": "424242+claude-autopilot[bot]@users.noreply.github.com"})
        self.assertEqual(kc.items["bot-user-id"], "424242")

    def test_auth_check_lists_the_installed_repos(self):
        out = apg.GitHubApp(keychain(), FakeHTTP(), now=lambda: NOW).auth_check()
        self.assertEqual(out["repos"], ["example-org/a", "example-org/b"])
        self.assertEqual(out["bot"], "claude-autopilot[bot]")


class WrapperTests(unittest.TestCase):
    IDENT = {"name": "claude-autopilot[bot]", "email": "1+claude-autopilot[bot]@users.noreply.github.com"}

    def test_gh_gets_the_token_in_its_environment_only(self):
        cmd, env = apg.gh_command(["pr", "create", "--title", "x"], "ghs_t", {"PATH": "/bin", "GITHUB_TOKEN": "ghp_owner", "GH_TOKEN": "gho_owner"})
        self.assertEqual(cmd, ["gh", "pr", "create", "--title", "x"])
        self.assertEqual(env["GH_TOKEN"], "ghs_t")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("ghs_t", " ".join(cmd))

    def test_git_commit_is_authored_by_the_bot_and_gets_no_token(self):
        cmd, env = apg.git_command(["-C", "/wt", "commit", "-m", "x"], self.IDENT, None, {"PATH": "/bin", "AUTOPILOT_GIT_TOKEN": "stale"})
        self.assertEqual(cmd, ["git", "-C", "/wt", "commit", "-m", "x"])
        self.assertEqual((env["GIT_AUTHOR_NAME"], env["GIT_COMMITTER_EMAIL"]), (self.IDENT["name"], self.IDENT["email"]))
        self.assertNotIn("AUTOPILOT_GIT_TOKEN", env)

    def test_git_push_resets_credential_helpers_and_reads_the_token_from_env(self):
        cmd, env = apg.git_command(["-C", "/wt", "push", "-u", "origin", "b"], self.IDENT, "ghs_t", {})
        self.assertEqual(cmd[:5], ["git", "-c", "credential.helper=", "-c", f"credential.helper={apg.CREDENTIAL_HELPER}"])
        self.assertEqual(cmd[-6:], ["-C", "/wt", "push", "-u", "origin", "b"])
        self.assertEqual(env["AUTOPILOT_GIT_TOKEN"], "ghs_t")
        self.assertNotIn("ghs_t", " ".join(cmd))

    def test_only_network_subcommands_get_the_token(self):
        self.assertEqual(apg.git_subcommand(["-C", "/wt", "-c", "a=b", "push"]), "push")
        self.assertEqual(apg.git_subcommand(["--no-pager", "log"]), "log")
        calls = []
        app = type("A", (), {"identity": lambda s: self.IDENT, "token": lambda s: calls.append(1) or "ghs_t"})()
        for args, wants in ((["-C", "/wt", "commit", "-m", "x"], False), (["-C", "/wt", "push"], True), (["fetch", "origin"], True)):
            seen = {}
            apg.run(["git", *args], app=app, execvpe=lambda f, a, e: seen.update(env=e), environ={})
            self.assertEqual("AUTOPILOT_GIT_TOKEN" in seen["env"], wants, args)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_ssh_remotes_are_rewritten_to_https_so_the_app_token_is_used(self):
        cmd, env = apg.git_command(["ls-remote", "--get-url", "git@github.com:example-org/web.git"], self.IDENT, "ghs_t",
                                   {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")})
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "https://github.com/example-org/web.git")
        cmd, env = apg.git_command(["ls-remote", "--get-url", "ssh://git@github.com/o/r.git"], self.IDENT, "ghs_t", {"PATH": os.environ["PATH"]})
        self.assertEqual(subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout.strip(), "https://github.com/o/r.git")

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_the_credential_helper_answers_git_with_the_env_token(self):
        cmd, env = apg.git_command(["credential", "fill"], self.IDENT, "ghs_from_env", {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")})
        out = subprocess.run(cmd, input="protocol=https\nhost=github.com\n\n", env=env, capture_output=True, text=True, check=True).stdout
        self.assertIn("username=x-access-token\n", out)
        self.assertIn("password=ghs_from_env\n", out)

    def test_unknown_command(self):
        with self.assertRaises(apg.GitHubError):
            apg.run(["frobnicate"], app=object())


if __name__ == "__main__":
    unittest.main()
