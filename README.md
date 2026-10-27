<p align="center"><img src="assets/logo.svg" width="112" alt="Autopilot logo"></p>

# Autopilot

A Linear agent that works the issues you **delegate** to it. It runs Claude Code on the owner's Mac
and posts everything in Linear **as Autopilot**: comments, agent activities and status changes. On
GitHub it is the app **`claude-autopilot[bot]`**: it authors its PRs, and an `@claude-autopilot`
mention on one of them gives it work, like a mention in Linear.

Delegate an issue to Autopilot. The next run takes it and does the full loop:
implement → PR → CI → AI review → merge → preprod verify.
It finishes the issue, or asks you one clear question in the issue.

When the Claude subscription hits its limit, or you choose not to use it, **API mode** carries on with
OpenRouter models under spend caps you set ([below](#non-claude-models-and-api-mode)).

Contents: [Using it](#using-it) · [Repository](#repository) · [Commands in Claude](#commands-in-claude) ·
[Non-Claude models and API mode](#non-claude-models-and-api-mode) · [Helpers](#helpers) ·
[Settings, state and logs](#settings-state-and-logs) · [Environment variables](#environment-variables) ·
[Troubleshooting](#troubleshooting) · [Tests](#tests)

## Using it

| You want | Do |
|---|---|
| Autopilot to work an issue | Delegate it to **Autopilot** in Linear (you stay the assignee) |
| To answer its question | Reply in the issue's Autopilot thread, or mention `@Autopilot` |
| Changes on its PR | Comment `@claude-autopilot …` on the PR |
| A non-Claude model to write one ticket's code | A `Coder model: <model>` line in the description, or a `coder:<model>` label |
| To stop it for a while | `/autopilot:pause`, then `/autopilot:resume` |
| To use no Claude subscription tokens | `/autopilot:api-mode on` |
| To see what it's doing | `/autopilot:status`, the issue's Autopilot session in Linear, `~/.local/state/autopilot/reports/` |

Runs start from three Claude Code desktop scheduled tasks (`autopilot-agent`, `-2`, `-3`, every 5 minutes;
[setup](docs/setup.md#4-schedule-it)), so up to 3 tickets are worked side by side. Each run takes one issue.
In API mode, a launchd tick and a supervisor start the runs instead ([below](#non-claude-models-and-api-mode)).
Merges reach preprod only; prod is released by hand. Setting it up for a company, step by step:
[docs/company-setup.md](docs/company-setup.md) (reference: [docs/setup.md](docs/setup.md)).

## Repository

| Path | What |
|---|---|
| `skills/orchestrator/` | one unattended run: gate, lock, intake, claim, outcome, report; its "API mode runs" rules |
| `skills/worker/` | one issue end to end, driven by the project's `autopilot.yaml` |
| `commands/` | the slash commands below |
| `hooks/hooks.json` | plugin hooks: `bin/autopilot-stream` (live Linear mirror) and `bin/autopilot-runstate` (usage limits) |
| `bin/autopilot-linear` | Linear GraphQL + gateway helper (stdlib Python). Tokens from Keychain only |
| `bin/autopilot-github` | GitHub as the app: installation tokens from the Keychain key; `gh` / `git` wrappers ([setup](docs/setup.md#github-app-autopilots-github-identity-since-050)) |
| `bin/autopilot-inflight` | Autopilot's open PRs grouped by issue, recorded blocks, run slots and locks: "finish before start" for up to 3 runs side by side (reads with the owner's `gh`) |
| `bin/autopilot-clobber` | Base guard before every merge: refuses a diff that deletes lines another change landed on the base after this PR started (`check`), and replays that test over a repo's merged PRs (`audit`). Read-only |
| `bin/autopilot-coder` | The implementation step on a non-Claude model through OpenRouter, contained to the worktree; also the shared OpenRouter config and spend ledger |
| `bin/autopilot-fallback` | API mode: the supervisor that runs Autopilot on OpenRouter, a model per role, under per-ticket and daily caps; the switch, the settings and the launchd tick |
| `bin/autopilot-stream` | Hook: mirrors a run live into its issue's Linear agent session, every message and tool call, headed by the agent that runs it; only in sessions holding an Autopilot issue lock |
| `bin/autopilot-runstate` | Hook: after a Claude usage limit or outage, frees the run's issue and slot; on a subscription limit, switches to API mode |
| `bin/autopilot-github-setup` | owner-run: creates the GitHub App from a manifest, stores its secrets |
| `bin/autopilot-gateway-setup` | owner-run: stores the gateway's secrets in Keychain and as Worker secrets |
| `gateway/` | Cloudflare Worker + D1: Linear agent-session and GitHub mention webhooks, instant "Queued" ack, job queue ([docs](docs/gateway.md)) |
| `schema/autopilot.schema.json` | per-project config schema |
| `examples/` | a generic `web-app` config and the `sandbox` smoke-test config. Real project configs live in `~/.config/autopilot/projects/`, never here |
| `assets/` | the logo (`logo.svg`) |
| `docs/` | [company setup guide](docs/company-setup.md), [setup](docs/setup.md), [gateway](docs/gateway.md), [architecture + decisions](docs/architecture.md), [lessons learned](docs/lessons-learned.md) |

## Commands in Claude

```
/autopilot:status                      # identity, GitHub app, queue, locks, flags, API mode, last report
/autopilot:run                         # one run now, like a scheduled one
/autopilot:run ENG-150                 # one run now, for this delegated issue
/autopilot:pause                       # scheduled runs exit at once (a run in progress finishes its issue)
/autopilot:resume                      # back on
/autopilot:api-mode                    # API mode status: active or not and why, models, today's spend, tick
/autopilot:api-mode on                 # use no subscription tokens: OpenRouter works the queue
/autopilot:api-mode off                # back to Claude (a usage limit still switches it on by itself)
/autopilot:api-mode set usd_per_day 30 # change a model or a cap, validated within bounds
```

## Non-Claude models and API mode

Two features use OpenRouter. Both need the key in the Keychain, and every dollar they spend is booked in
Autopilot's own ledger: the caps come from Autopilot's settings, not from the key's limit on OpenRouter.

- **Coding on another model, per ticket.** A `Coder model: <model>` line (or a `coder:<model>` label) has
  that model write the code through `bin/autopilot-coder`, which can only read and edit files in the
  worktree. Planning, review, merge and Linear stay on Claude.
- **API mode, for whole tickets.** It turns on by itself when the Claude subscription hits its 5-hour
  or weekly limit, and stays on until Claude works again. Or turn it on yourself. A model per role:

| Role | Default | Does |
|---|---|---|
| Orchestrator | `openai/gpt-5.6-terra` | the run itself: picks the issue, follows the skills, Linear, GitHub, merge |
| Reviewer | `openai/gpt-5.6-sol` | every reviewer subagent, the security reviewer included (a different model family on purpose) |
| Coder | `deepseek/deepseek-v4-pro` | writes the code through `bin/autopilot-coder` |

Default caps: **$5 per ticket** (the run is stopped past it) and **$20 per day** (nothing starts past it).
Most of a run's cost is the orchestrator: it re-reads the skills on every turn. In the first real test
(on GPT-5.6 Terra) the main run cost $0.73, of which $0.06 was the coding; the whole ticket $0.98.

How it runs: a **launchd tick** (macOS timer, every 5 minutes, no tokens) starts the **supervisor**
(plain Python, no tokens) while API mode is active. The supervisor starts one headless
`claude -p` **run** at a time on the orchestrator model and watches its spend. With an empty queue
nothing is started and nothing is spent. When runs stop making progress, it gives up until the queue
changes or 6 hours pass.

Set up once (Terminal):

```bash
security add-generic-password -U -s autopilot-openrouter -a api-key -w   # store the OpenRouter key (prompts)
~/.claude/plugins/marketplaces/autopilot/bin/autopilot-fallback install-tick   # the launchd tick
```

Day to day, without Claude (`F=~/.claude/plugins/marketplaces/autopilot/bin/autopilot-fallback`):

```bash
$F status                                         # active and why, models, today's spend, run going, tick installed
$F mode on                                        # API mode on: OpenRouter works the queue
$F mode off                                       # off (a usage limit still switches it on by itself)
$F config                                         # all settings, their bounds, today's spend
$F config set orchestrator openai/gpt-5.6-terra   # the default: followed the skills reliably
$F config set orchestrator openai/gpt-5.6-luna    # about 10x cheaper with caching, but slipped on steps
$F config set orchestrator deepseek/deepseek-v4-pro   # cheap on paper; uncached on OpenRouter's route
$F config set reviewer openai/gpt-5.6-sol         # the reviewer model
$F config set coder deepseek/deepseek-v4-pro      # the default coder model
$F config set usd_per_ticket 3                    # cap per run, 0.01-50
$F config set usd_per_day 30                      # cap per UTC day for all OpenRouter spend, 0.01-500
$F config set timeout_min 90                      # a run's time limit, 5-600
$F config set permission_mode dontAsk             # the default: only the allow list runs (auto refuses reviews and merges)
$F config set allowed_tools '["Bash(npm test:*)"]'  # extra permission rules for runs (a JSON list)
$F start                                          # start the supervisor now (if active and none runs)
$F tick                                           # what the launchd tick runs
$F limited                                        # record a Claude usage limit by hand
$F clear-limit                                    # forget it (Claude works again)
$F uninstall-tick                                 # remove the launchd tick
tail -f ~/.local/state/autopilot/fallback.log     # what API-mode runs are doing
```

Model names: `bin/autopilot-coder model <name>` says which OpenRouter id a name means
(`DeepSeek flash 4.1` → `deepseek/deepseek-v4.1-flash`) and refuses names that match several.
Claude models are never accepted: they run on the subscription. Full details: [docs/setup.md](docs/setup.md#api-mode).

## Helpers

All helpers print JSON on stdout and exit 1 on an error, 2 when setup is missing. They read secrets
from the Keychain only and never print them. Run them from the repo (`bin/…`) or from the installed
plugin (`~/.claude/plugins/marketplaces/autopilot/bin/…`).

### autopilot-linear (Linear, as the Autopilot app)

```bash
bin/autopilot-linear auth-check                 # who the token acts as (must be the app)
bin/autopilot-linear whoami                     # the app user (its id is the gateway's APP_USER_ID)
bin/autopilot-linear users                      # active people and their ids (for ALLOWED_USER_IDS)
bin/autopilot-linear queue [--next|--count|--list] [--queued] [--project P] [--team T] [--exclude-project P]
                                                # gateway jobs (or delegated issues), in run order
bin/autopilot-linear jobs next|get|ack|done|blocked [JOB_ID] [--note "..."]
bin/autopilot-linear issue ENG-150              # issue, comments, deps, latest Autopilot session
bin/autopilot-linear comment ENG-150 --body "..."          # or --body-file PATH (- for stdin)
bin/autopilot-linear ask-owner ENG-150 --body-file PATH [--fallback "<owner>"]   # top-level comment that @mentions the owner
bin/autopilot-linear activity ENG-150 --type thought|response|elicitation|error --body "..."
bin/autopilot-linear activity ENG-150 --type action --action "Opened PR" --parameter repo --result URL
bin/autopilot-linear state ENG-150 "In Review" [--settle 20]   # --settle: set it again if Linear moved it meanwhile
bin/autopilot-linear automations --team ENG     # Linear PR automations that would fight Autopilot's statuses
bin/autopilot-linear link ENG-150 --label "PR" --url https://github.com/... [--session ID]
bin/autopilot-linear create --team ENG --project "X" --title "Follow-up" [--state S] [--parent ID] --body-file PATH
```

### autopilot-inflight (what's in flight, blocks, slots, locks)

```bash
bin/autopilot-inflight sweep [--repo OWNER/NAME ...]   # capacity, next in-flight item, deferred work, stalled items
bin/autopilot-inflight count [--repo ...]              # one number: in-flight work a run can act on
bin/autopilot-inflight claim ENG-1                     # take in-flight work back (counts an automatic retry)
bin/autopilot-inflight wait ENG-1 --kind owner|checkable|slot|claude --reason "..." --repo OWNER/NAME [--after IDS]
                                                       # record a block and what unblocks it
bin/autopilot-inflight clear ENG-1                     # forget a block (done, or the owner answered)
bin/autopilot-inflight notified ENG-1 --reason "..."   # the owner was told about this stall
bin/autopilot-inflight interrupted ENG-1               # how often a usage limit or outage cut its runs off
bin/autopilot-inflight slot take [--agent NAME] | slot touch N --token T | slot release N --token T | slot busy
bin/autopilot-inflight lock NAME [--wait SECONDS] | lock NAME --touch --token T | unlock NAME --token T
                                                       # NAME: an issue id, "verify" (preprod browser) or "keepalive"
bin/autopilot-inflight overlap ENG-1 --files PATH ...  # other in-flight issues touching the same files
bin/autopilot-inflight plugin-check                    # was Autopilot updated after this run started?
```

Wait kinds: `owner` (only your answer unblocks it), `checkable` (resumes when PRs, base or checks change),
`slot` (waits for other in-flight tickets), `claude` (a step an API-mode run left for the next Claude run).
Up to `AUTOPILOT_MAX_RUNS` (3) runs side by side; a lock or slot untouched for 2 hours is taken over.

### autopilot-github (GitHub, as the Autopilot app)

```bash
bin/autopilot-github auth-check                 # the bot and the repos the app is installed on; exit 2 = not set up
bin/autopilot-github identity                   # name and e-mail commits are authored with
bin/autopilot-github gh pr create ...           # gh with an installation token: the PR is the bot's
bin/autopilot-github git -C WT commit|push ...  # git as the bot; push/fetch authenticate as the app
```

### autopilot-clobber (base guard, read-only)

```bash
bin/autopilot-clobber check --repo-dir DIR --base REF --head REF (--since ISO8601 | --github OWNER/REPO --pr N) [--allow SHA ...]
                                                # exit 0 clean, 3 deletes newer work, 4 HEAD is behind the base
bin/autopilot-clobber audit --repo-dir DIR --github OWNER/REPO [--limit N]   # replay the test over merged PRs
```

### autopilot-coder (code on a non-Claude model)

```bash
bin/autopilot-coder check                                   # the key works; key usage, today's spend, daily cap
bin/autopilot-coder model DeepSeek flash 4.1 [--aliases '{"deepseek": "deepseek/deepseek-v4.1-flash"}']
bin/autopilot-coder run --model ID --cwd WORKTREE --max-usd N [--timeout-min 60] [--out FILE] < prompt
                                                            # minutes long: start it in the background
```

### Setup scripts (owner-run, in your own Terminal)

```bash
bin/autopilot-github-setup create-app [--name NAME] [--org ORG]   # create the GitHub App from a manifest
bin/autopilot-github-setup worker-secrets                          # copy its key and webhook secret to the Worker
bin/autopilot-gateway-setup all https://<gateway>.workers.dev      # gateway URL, runner token, Linear client, webhook secret
bin/autopilot-gateway-setup url|runner-token|linear-client|webhook-secret   # one of them
```

## Settings, state and logs

| Where | What |
|---|---|
| `~/.config/autopilot/projects/<name>.yaml` | per-project config ([schema](schema/autopilot.schema.json), [examples](examples/)) |
| `~/.config/autopilot/openrouter.json` | API mode and coding models, caps, permission rules (`$F config set`) |
| `~/.claude/scheduled-tasks/autopilot-agent*/SKILL.md` | the three scheduled tasks' prompts |
| `~/Library/LaunchAgents/local.autopilot.fallback.plist` | the launchd tick |
| Keychain services `autopilot-linear`, `-github`, `-gateway`, `-cloudflare`, `-openrouter` | every secret ([list](docs/setup.md#keychain-items)) |

State, in `~/.local/state/autopilot/`:

| File | Meaning |
|---|---|
| `paused` | runs exit at once (`/autopilot:pause`) |
| `api-mode` | API mode switched on by hand |
| `claude-limited` | a Claude usage limit is in force (cleared when Claude runs a command again) |
| `fallback.lock` | the supervisor holds it while it runs (with its pid) |
| `fallback-giveup.json` | the supervisor gave up; it isn't restarted until the queue changes or 6 hours pass |
| `openrouter-spend.json` | the spend ledger: per UTC day, by source (`api_mode`, `coder`) |
| `slots/`, `locks/` | run slots and issue / `verify` / `keepalive` locks |
| `waiting/<ID>.json` | a recorded block and what unblocks it |
| `runs/`, `streams/`, `current-jobs/`, `attempts/` | per-run hook state, claimed jobs, attempt counts |
| `auth-broken`, `github-broken`, `<project>.preprod-broken`, `linear-automations` | problems runs found; `/autopilot:status` explains them |
| `<project>.keepalive`, `<project>.signin-notice`, `<project>.signin-failed` | the last preprod keep-alive, the open sign-in notice, the last failed Passhand sign-in |
| `reports/YYYY-MM-DD.md` | the daily report (or each config's `reports.dir`) |
| `fallback.log`, `fallback-tick.log` | API mode: every run's output and cost; what the tick started |
| `runstate.log`, `stream.log` | the hooks' own errors (they never fail a run) |

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `AUTOPILOT_MAX_RUNS` | 3 | runs side by side |
| `AUTOPILOT_STATE_DIR` | `~/.local/state/autopilot` | state folder |
| `AUTOPILOT_CONFIG_DIR` | `~/.config/autopilot/projects` | project configs |
| `AUTOPILOT_OPENROUTER_CONFIG` | `~/.config/autopilot/openrouter.json` | OpenRouter settings |
| `AUTOPILOT_GATEWAY` | | `off`: poll Linear instead of the gateway |
| `AUTOPILOT_GATEWAY_URL` | | a local `wrangler dev` gateway (localhost only) |
| `AUTOPILOT_STREAM` | | `0`: no live mirror into Linear |
| `AUTOPILOT_CLAUDE_BIN` | `claude` on PATH, else `~/.local/bin/claude` | the CLI the coder and API-mode runs start |
| `AUTOPILOT_LINEAR_SCOPES` | `read,write,app:assignable,app:mentionable` | scopes of the app token |
| `AUTOPILOT_FALLBACK`, `AUTOPILOT_CODER_MODEL`, `AUTOPILOT_FALLBACK_USD` | | set by the supervisor inside an API-mode run; don't set them yourself |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Nothing picks up a delegated issue | Check the scheduled tasks ran recently (Routines in the Claude app): the app only starts them while it's open and the Mac is awake. `/autopilot:status` shows the queue and flags |
| An issue sits in Queued after your reply | A run is still going, or (API mode) a supervisor is waiting on a run: `$F status`, `tail ~/.local/state/autopilot/fallback.log` |
| API mode doesn't start | `$F status`: `active` null means neither the switch nor a limit is on; check `fallback-giveup.json` (`$F mode on` retries at once), the day's cap, the key (`bin/autopilot-coder check`) |
| An API-mode run is refused a command | Runs use `dontAsk`: only the allow list runs. Add what your repo's checks need with `$F config set allowed_tools '[...]'`; rules match exact words, no `*` inside a path, and a command with `if`/`for`, `$(…)` or a heredoc never matches |
| A run used the wrong model | `$F config` shows the models in use. Known gap: in API mode a ticket's `Coder model:` line is not yet honoured, so the coder is always the `coder` setting |
| Spend looks wrong | `~/.local/state/autopilot/openrouter-spend.json` is the record; with several runs at once a run can book the others' spend too (it overstates, never understates). A damaged ledger blocks all spend until you fix or remove it |
| Preprod verification keeps asking you to sign in | Opt in to `preprod.sign_in` so runs sign the test user back in with Passhand ([docs/setup.md](docs/setup.md#5-preprod-sign-in-with-passhand-optional-since-0170)). Already on: the sign-in notice quotes Passhand's message; `/autopilot:status` shows `signin-failed` |
| The Mac is off | Nothing runs; see [docs/backup-runner.md](docs/backup-runner.md) (proposal) |

## Tests

```bash
python3 -m unittest discover -s tests            # unit tests, stdlib only
pip install pyyaml jsonschema graphql-core       # + config schema and Linear GraphQL contract tests
(cd gateway && npm test)                         # gateway Worker, node --test, no dependencies
```

## License

Source-available, not open source: you may read the code and run unmodified copies, including
for your company, but not modify, redistribute or claim it. See [LICENSE](LICENSE).
