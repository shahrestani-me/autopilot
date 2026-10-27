# Setup

Do this once per Linear workspace, then once per Mac that runs Autopilot.
Secrets go only into the macOS Keychain. Never paste them into chat, files, issues or logs.

## 1. Create the Linear app (workspace admin, ~5 min)

1. Open <https://linear.app/settings/api/applications/new>.
   - **Name:** Autopilot. Add an icon.
   - **Developer URL:** `https://github.com/shahrestani-me/autopilot`
   - **Callback URL:** `http://localhost:8765/callback`
   - **Client credentials:** ON. The runner mints its own app tokens with them.
   - **Webhooks:** leave them off until the gateway is deployed; then follow [gateway.md](gateway.md).
2. Store the credentials in the Keychain. Run this in your own Terminal: `-w` as the last option
   **prompts** for the value, so it never lands in shell history.

   ```bash
   security add-generic-password -U -s autopilot-linear -a client-secret -w
   ```

   The client ID isn't secret:

   ```bash
   security add-generic-password -U -s autopilot-linear -a client-id -w <CLIENT_ID>
   ```
3. Install the app into the workspace **as an agent**. Open this URL, choose the team(s), approve:

   ```
   https://linear.app/oauth/authorize?client_id=<CLIENT_ID>&redirect_uri=http://localhost:8765/callback&response_type=code&actor=app&scope=read,write,app:assignable,app:mentionable
   ```

   The browser then fails to load `localhost`. That's expected: the install is what matters, and
   the code in the URL is never used.
4. In the app's settings, check that **team access** includes every team Autopilot should work in.

## 2. Check it

```bash
bin/autopilot-linear auth-check
```

It should print `"ok": true` and the Autopilot app user as `viewer`. The first call mints an
app token (client_credentials, actor=app, 30 days) and caches it in the Keychain as
`autopilot-linear / access-token`, with its expiry in the item's comment. Later calls reuse it
and mint a new one a day before expiry, or after a 401.

## Why client credentials, not a pasted token

Linear OAuth access tokens live 24 h and their refresh tokens rotate on every use. One token
pasted once would stop working the next day, and a rotating refresh token is easy to lose on an
unattended Mac. A client-credentials token is an `app` actor token valid 30 days, and the runner
can mint a fresh one at any time from the client secret.

Two things to know:
- Requesting a **different scope set** revokes every existing app token. The scope string is fixed in
  `bin/autopilot-linear` (`SCOPES`); don't change it casually.
- **Rotating the client secret** invalidates the tokens. Store the new secret with the same command,
  and the next run mints a new token.

## GitHub App (Autopilot's GitHub identity, since 0.5.0)

Autopilot's branches, commits, PRs, PR comments and merges show as **`<slug>[bot]`**, a
private GitHub App you create on your org (e.g. `claude-autopilot`), not as the owner's
account. Mentioning `@<slug>` on one of its PRs gives it work (docs/gateway.md, "GitHub").
Without the app on a Mac (no Keychain items), runs fall back to the owner's own `gh` login, as before.

**Create it (owner, once, ~3 min).** In your own Terminal, from the repo:

```bash
AUTOPILOT_GITHUB_ORG=<your-org> bin/autopilot-github-setup create-app [--name <app-name>]
```

It opens a local page that sends GitHub the app manifest; you review it on GitHub and click
**Create GitHub App**. GitHub hands a one-time code back to the script, which converts it into the
app's id, slug, private key and webhook secret and writes them straight to the Keychain (service
`autopilot-github`, through `security -i` stdin). Nothing secret is printed or written to a file;
the user-sign-in client secret is dropped (Autopilot never signs users in).

Then **install** it (`https://github.com/apps/<slug>/installations/new`): your org
→ *Only select repositories* → the repos in any config's `repos`. Add a repo there before a config uses it.

Then copy the key and webhook secret to the gateway: `bin/autopilot-github-setup worker-secrets`.

**Permissions, and why each one** (keep `PERMISSIONS` in `bin/autopilot-github-setup` in step):

| Permission | Access | Why |
|---|---|---|
| Contents | read & write | push Autopilot's branches; squash-merge with `--delete-branch` |
| Pull requests | read & write | open, edit and merge PRs; read reviews and review comments (the merge-hold check) |
| Issues | read & write | PR conversation comments are issue comments: reply "Queued", react 👀, post the outcome |
| Checks | read | `gh pr checks --watch` |
| Actions | read | `gh run watch`, `gh run view --log-failed` (CI and the preprod deploy) |
| Workflows | read & write | push changes under `.github/workflows/` (owner's choice: parity with their `gh` token) |
| Metadata | read | mandatory for every app |

**Webhook events:** `issue_comment`, `pull_request_review`, `pull_request_review_comment`, sent to
`<gateway>/github` and signed with the webhook secret (`X-Hub-Signature-256`).

**Check it:** `bin/autopilot-github auth-check` prints `"ok": true`, the bot and the installed repos.
The first call mints an installation token (RS256 JWT from the key → `POST /app/installations/<id>/access_tokens`,
valid 1 h) and caches it in the Keychain with its expiry; later calls reuse it until 10 minutes before.

**How runs use it.** `bin/autopilot-github gh <args>` runs `gh` with `GH_TOKEN` set to the token, in
that process's environment only. `bin/autopilot-github git <args>` runs `git` with the bot as author
and committer; `push`/`fetch`/`pull`/`ls-remote`/`clone` also get a credential helper that reads the
token from the environment (every configured helper is reset first, so osxkeychain never stores it).
Other git commands, which may run repo hooks, never see the token.

**Merging.** Both main rulesets require 0 approvals. Their preview flag
`require_extra_approval_for_unattributed_changes` treats bot- and AI-authored commits as
"unattributed" and asks for an approval, which is why owner-authored PRs needed `--admin`. The
owner turned it off on the project repos (2026-10-19), so the app merges
normally and required checks (gitleaks) stay enforced. If a merge still asks for an approval, the run
blocks and asks the owner to approve: they can, since the PR is the app's. `merge.admin_on_missing_approval`
only applies without the app. The ruleset API sets the flag back to true when an update omits it:
always send it explicitly.

## OpenRouter (coding on non-Claude models, since 0.14.0, optional)

An issue with a `coder:<model>` label, or a `Coder model: <model>` line in its description or an
owner comment, gets its code written by that model through OpenRouter (booked against the daily cap
in [API mode](#api-mode)'s settings); the rest of the run stays
on Claude (worker section 3). Nothing else switches it: a `Recommended model:` line or a model the
work is about doesn't. Without a key such an issue blocks and asks for one; issues without the
marker work exactly as before.

1. Create a key at <https://openrouter.ai/settings/keys> with a **credit limit** (e.g. $10): it's the
   hard stop on top of `spend.paid_model_usd_per_ticket`.
2. Store it (prompts for the value):

   ```bash
   security add-generic-password -U -s autopilot-openrouter -a api-key -w
   ```
3. `bin/autopilot-coder check` prints `"ok": true` and the key's usage.

`bin/autopilot-coder run` starts `claude -p` in the worktree with `ANTHROPIC_BASE_URL=https://openrouter.ai/api`
and the key in that process's environment only, every model tier set to the chosen model. In
`dontAsk` mode it may only Read/Edit/Write (and Glob/Grep) inside the worktree: no Bash, web, MCP
servers or subagents, so text in a ticket can't steer it to a Keychain item or a push. The Claude run
runs the checks and hands failures back. Spend is the key's usage before and after, read every 20 s;
the model is stopped when the run goes over `--max-usd`, on its timeout, or when usage can't be read.

## API mode

When Claude can't run Autopilot, OpenRouter models can (since 0.15.0). It turns on by itself when the
Claude subscription hits its usage limit (the StopFailure hook), or by hand: `/autopilot:api-mode on`
(`off` to end it, no argument for status). It needs the OpenRouter key above.

| Role | Default model | Does |
|---|---|---|
| Orchestrator | `openai/gpt-5.6-terra` | the run itself: picks the issue, follows the skills, Linear, GitHub, merge (cheaper ones, Luna and DeepSeek V4 Pro, slipped on the skills' steps in the first tests) |
| Reviewer | `openai/gpt-5.6-sol` | every reviewer subagent, the security reviewer included |
| Coder | `deepseek/deepseek-v4-pro` | writes the code through `bin/autopilot-coder`, contained to the worktree |

A detached supervisor (`bin/autopilot-fallback`) works the queue one run at a time while API mode is
active. A launchd tick, every 5 minutes, starts it again whenever API mode is active and none runs,
so Autopilot keeps going through a **weekly** limit too, without any Claude session having to run.
Install it once (it records your `PATH`, so runs find `gh`, `git` and `claude`):

```bash
~/.claude/plugins/marketplaces/autopilot/bin/autopilot-fallback install-tick
```

A limit, the 5-hour one or the weekly one, holds until Claude works again: the next Claude session
that runs a command clears it (a limit set in the last 10 minutes stays, as other sessions may not
have reached it yet). With API mode switched on by hand, the scheduled tasks print `SKIP-API` and
use no subscription tokens beyond that check. API-mode runs skip the preprod keep-alive, and a
preprod check that needs the owner's browser is left to the next Claude run (`wait: claude`).

**Spend is Autopilot's own.** Every OpenRouter dollar, API-mode runs and `Coder model:` coding alike,
is booked in `~/.local/state/autopilot/openrouter-spend.json` (per UTC day, by source), and the caps
are enforced from it: a run is stopped once it passes `usd_per_ticket`, and nothing starts once the
day has spent `usd_per_day`. The key's credit limit on OpenRouter is only a backstop. A damaged
ledger blocks all spend until you fix or remove it. When runs stop making progress (what's left
needs Claude or you) or keep ending at once, the supervisor gives up, and neither the tick nor the
hook restarts it until the queue changes or 6 hours pass; `/autopilot:api-mode on` retries at once.

Settings live in `~/.config/autopilot/openrouter.json`; every key is optional:

| Key | Default | Bounds |
|---|---|---|
| `orchestrator`, `reviewer`, `coder` | the models above | any non-Claude OpenRouter id |
| `usd_per_ticket` | 5 | 0.01–50 |
| `usd_per_day` | 20 | 0.01–500 |
| `timeout_min` | 120 | 5–600 |
| `permission_mode` | `dontAsk` | `dontAsk` (only the allow list runs, the rest is refused) or `auto`, `acceptEdits`, `bypassPermissions`. Keep `dontAsk`: auto mode's check refuses an agent's own review and merge as self-approval |
| `allowed_tools` | `[]` | up to 50 extra permission rules for API-mode runs, e.g. `"Bash(npm test:*)"` |

Change one with `/autopilot:api-mode set <key> <value>`, or without Claude:

```bash
~/.claude/plugins/marketplaces/autopilot/bin/autopilot-fallback config set usd_per_day 30
```

A headless run has none of the approvals you gave the scheduled tasks in the app, so the
supervisor passes its own: Autopilot's helpers at the installed plugin's exact paths, `git -C`,
`git fetch`/`worktree`, read-only `gh pr`/`gh run` commands, read-only shell tools, and the state
flags the skills clear. Everything else goes through auto mode's check; add what your repos'
`checks` need to `allowed_tools` (a rule matches exact words: no wildcards inside a path).

It's validated before it's written; `autopilot-fallback config` shows the settings and today's
spend. The supervisor logs to `~/.local/state/autopilot/fallback.log`, the tick to `fallback-tick.log`.

## Keychain items

| Service | Account | Written by | Holds |
|---|---|---|---|
| `autopilot-linear` | `client-id` | owner | OAuth client ID |
| `autopilot-linear` | `client-secret` | owner | OAuth client secret |
| `autopilot-linear` | `access-token` | `autopilot-linear` | cached app token; comment `exp=<epoch>` |
| `autopilot-cloudflare` | `deploy-token` | owner | scoped Cloudflare API token (Workers Scripts + D1 edit) for deploys |
| `autopilot-gateway` | `url` | owner (`bin/autopilot-gateway-setup`) | gateway URL (not secret) |
| `autopilot-gateway` | `token` | owner (`bin/autopilot-gateway-setup`) | shared runner token, also the Worker's `GATEWAY_TOKEN` |
| `autopilot-gateway` | `webhook-secret` | owner (`bin/autopilot-gateway-setup`) | Linear's webhook signing secret, also a Worker secret |
| `autopilot-github` | `app-id`, `app-slug`, `owner` | owner (`bin/autopilot-github-setup`) | the GitHub App's id, slug and org (not secret) |
| `autopilot-github` | `private-key` | owner (`bin/autopilot-github-setup`) | the app's private key, base64 PKCS#8 DER; also the Worker's `GITHUB_APP_PRIVATE_KEY` |
| `autopilot-github` | `webhook-secret` | owner (`bin/autopilot-github-setup`) | the app's webhook secret, also the Worker's `GITHUB_WEBHOOK_SECRET` |
| `autopilot-github` | `installation-token` | `autopilot-github` | cached installation token (1 h); comment `exp=<epoch>` |
| `autopilot-github` | `bot-user-id` | `autopilot-github` | the bot user's id, for the commit e-mail (not secret) |
| `autopilot-openrouter` | `api-key` | owner | OpenRouter key for coding on non-Claude models (optional) |

All items are created by `/usr/bin/security`, so reading them back with the same tool doesn't prompt.
The helper passes values to `security` on stdin, never on the command line.

## 3. Install the plugin in Claude Code

```bash
claude plugin marketplace add shahrestani-me/autopilot
claude plugin install autopilot@autopilot
```

Then add a project config: copy an example from `examples/` to
`~/.config/autopilot/projects/<name>.yaml` and edit it. A project's config, skills and
house rules live outside this repo (in that folder or in the project's own repo), never in it. The schema is
`schema/autopilot.schema.json`.

## 4. Schedule it

A Claude Code desktop scheduled task, every 10 minutes, in Auto mode:

```
Autopilot run. Agent: autopilot-agent
First, in Bash:
  S=~/.local/state/autopilot; B=<plugin>/bin
  test -f $S/paused && echo SKIP-PAUSED
  test -f $S/api-mode && { $B/autopilot-fallback start --reason scheduled >/dev/null 2>&1; echo SKIP-API; exit 0; }
  BUSY=$($B/autopilot-inflight slot busy 2>/dev/null || echo 0)
  [ "$BUSY" -ge "${AUTOPILOT_MAX_RUNS:-3}" ] && echo SKIP-LOCKED
  for c in ~/.config/autopilot/projects/*.yaml; do grep -q '^  keepalive:' "$c" || continue
    k=$S/$(basename "$c" .yaml).keepalive; [ -n "$(find "$k" -mmin -1200 2>/dev/null)" ] || { echo KEEPALIVE-DUE; break; }; done
  $B/autopilot-linear queue --count --queued
  $B/autopilot-inflight count
If it printed SKIP-*, or both counts are 0 without KEEPALIVE-DUE, end immediately and say nothing more.
(SKIP-API: API mode is on, and the OpenRouter runs work the queue instead; see [API mode](#api-mode).)
Otherwise use the autopilot:orchestrator skill.
```

Make one task per run slot, `autopilot-agent`, `autopilot-agent-2` and `autopilot-agent-3`, with the
same prompt except the `Agent:` line, which names the task itself (0.12.0). The run passes it to
`slot take --agent`, and the plugin's hooks show it at the top of the issue's Linear agent session
("Run by Agent 2 (`autopilot-agent-2`) · Claude session `…`") and in a usage-limit note, so you can
go from Linear straight to that task's runs. A prompt without the line still works: its runs show
as manual runs.

The fast exit keeps idle runs to a single Bash call. The second count (0.6.0) is in-flight work a run
should act on without a job: green PRs to merge, red ones whose blocker may have cleared, deferred asks,
stalls to report. Without it a blocked ticket with green PRs waits until something else is queued.
Since 0.7.0 up to three runs work side by side (`AUTOPILOT_MAX_RUNS`), each in its own slot: the task fires
every 10 minutes and starts another run while a slot is free and there's work no run has taken
(`--queued` leaves out the jobs running runs hold; `inflight count` leaves out issues a run has locked). `KEEPALIVE-DUE` wakes an idle run when a config
with `preprod.keepalive` has no stamp, or one older than 20 hours (orchestrator section 1b). It finds
configs by file name, so name each file `<config name>.yaml`.

## 5. Preprod sign-in with Passhand (optional, since 0.17.0)

Without it, a run that finds the preprod login screen asks you to sign in (a sign-in notice in Linear),
and verification waits until you do. That happens every time the session ends, e.g. once a week on
an app whose sessions can't refresh. With `preprod.sign_in`, the run signs the test user back in with
[Passhand](https://github.com/shahrestani-me/passhand) and only asks you when that fails
(orchestrator section 1c).

Passhand fills a login saved on this Mac straight into the page in Chrome, chosen by the tab's real
origin. Autopilot calls its `fill_login` tool and never sees the password: it isn't in the chat, the
logs, Linear or the reports.

Set up once:

1. Install Passhand and its Chrome extension ([its README](https://github.com/shahrestani-me/passhand#install)),
   then restart the Claude app so the runs get its `fill_login` tool.
2. Save the test user's login and mark it `auto` (Terminal: it asks for the password without echoing it,
   and `--auto` asks for Touch ID):

   ```bash
   passhand add <preprod host> <test user's username> --auto
   ```

   `auto` fills without Touch ID, which a run needs because nobody is at the Mac; runs check it with
   `list_saved_logins` and don't fill a login that isn't `auto`
   ([Passhand on unattended logins](https://github.com/shahrestani-me/passhand#unattended-logins-test-and-qa-accounts)). Use it for **preprod
   test accounts only**: never a personal or production account, and never on a `forbidden.hosts` host.
   Accounts with 2FA or a passkey can't be signed in this way, nor a login form on another host than
   `preprod.web_url`'s (a separate identity provider): runs only fill on the preprod host itself.
3. In the project config:

   ```yaml
   preprod:
     browser: chrome
     sign_in:
       with: passhand
       user: test-user                     # a test_users name; default: keepalive.user
       username: test-user@example.com     # the username saved in Passhand, if it isn't the name
       keep_signed_in: Keep me logged in   # a checkbox to tick, if the login form has one
   ```

4. Keep the Mac awake and logged in, and Chrome open with the extension enabled.

Day to day: a failed sign-in writes `~/.local/state/autopilot/<config name>.signin-failed`, and runs
don't try again for 20 hours, so a wrong or missing login can't lock the account. The sign-in notice
then quotes Passhand's message. Fix the login (`passhand list`, `passhand add …`, `passhand auto …`) or
sign in by hand; the next keep-alive that finds the user signed in deletes the file and closes the notice.
