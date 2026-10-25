# Gateway (phase 2)

A Cloudflare Worker + D1 database (decision D1) between Linear and the Mac runner. Code: [`gateway/`](../gateway).

```
Linear ──AgentSessionEvent──▶ POST /webhook ──▶ D1 jobs ◀── GET /jobs/next ── runner (every 10 min)
   ▲                           verify signature              POST /jobs/:id/ack|done|blocked
   └── "Queued; I'll start within N minutes" (thought, as Autopilot, within seconds)
```

## What it does

- **`POST /webhook`** takes Linear's agent-session webhooks for the Autopilot app:
  - checks `Linear-Signature` (HMAC-SHA256 of the raw body with the signing secret) and that
    `webhookTimestamp` is under a minute old; anything else gets a 401;
  - ignores other event types and other apps' events (200);
  - `created` (a delegation or an @mention) and `prompted` (a reply in an Autopilot session) store a
    job: session id, issue, comment and the prompt text. A second reply while a job is still queued
    joins that job;
  - answers 200 at once, then posts a `thought` into the session as Autopilot (Linear wants one
    within 10 s): *"Queued; I'll start within N minutes"*, counted down from the runner's last poll.
    It says so instead when jobs are ahead, or when the runner hasn't polled for 30 min (Mac asleep);
  - only people in `ALLOWED_USER_IDS` (`wrangler.toml`: the owner) can start work; others get a
    short `response` and no job;
  - a session Autopilot opened itself is left alone (0.13.1). Its `created` event has no creator, like
    one from a Linear automation, so it counts as ours only when a job already holds the session or the
    runner announced it for that issue in the last 2 minutes (`POST /sessions/expect`). Answering it
    would also close it: the run's posts then kept opening new sessions, each answered the same way;
  - a **stop** signal cancels the session's queued jobs and confirms with a `response`. A running
    job gets `stopRequested`; the run checks it before opening and before merging a PR;
  - a session with no issue (a mention in a document) gets a short `response` and no job;
  - retries from Linear (1 min, 1 h, 6 h) never queue or acknowledge twice, and a failed write
    leaves nothing behind, so the retry still queues the job.
- **Runner API** (`Authorization: Bearer <GATEWAY_TOKEN>`):
  - `GET /jobs/next`: `{pending, job, jobs}`. An interrupted job (`running`; with runs side by side it may
    also be a live run's, so the runner takes it only when it gets the issue's lock) comes first, then the owner's messages about started work
    (`prompted`: replies, mentions in a session, PR mentions), then new work (`created`), oldest first
    in each (0.6.0: talking to Autopilot never waits behind new tickets). The "Queued behind N" thought
    counts in the same order. Also records the poll time.
  - **One live session per issue** (0.6.1): a second delegation or mention of an issue that already has
    an open job joins it. Queued: its note is added to the job's prompt. Running: a note becomes a
    `prompted` job in the running session, which the run reads at its next checkpoint. Either way the
    new session gets a `response` pointing to the earlier one. A PR mention on an issue being worked
    on goes into the running session too, instead of opening a new one.
  - `GET /jobs/:id`: one job (the stop checkpoint).
  - `POST /sessions/expect` `{"issueId": "<Linear issue UUID>"}`: the runner is about to open a session
    on that issue (`autopilot-linear`, when a run has no open session to post into). The next creator-less
    `created` event on the issue that no job holds uses it up; it expires after 2 minutes. The GitHub path
    announces its sessions the same way (their job usually claims the event first).
  - `POST /jobs/:id/ack`: `running`, `attempts + 1`. `done` / `blocked`: finished, with an optional
    `{"note": "..."}`; repeating the same one is fine. Anything else on a finished or cancelled job
    answers 409.
- `GET /health` says only `{"ok": true}`.

The Worker mints its own client-credentials app token (same scopes as the runner, so neither
revokes the other; Linear allows many in parallel) and caches it in D1's `state` table.

## GitHub (since 0.5.0)

- **`POST /github`** takes the `claude-autopilot` GitHub App's webhooks (docs/setup.md, "GitHub App"):
  - checks `X-Hub-Signature-256` (`sha256=` + HMAC-SHA256 of the raw body with the app's webhook
    secret); anything else gets a 401. `ping` answers 200;
  - takes `issue_comment` (created, on a PR), `pull_request_review` (submitted) and
    `pull_request_review_comment` (created) that **@mention the app** (`@claude-autopilot`,
    `GITHUB_APP_SLUG`). Everything else, and anything a bot sent (Autopilot's own replies come back
    as webhooks), is ignored with 200;
  - only logins in `ALLOWED_GITHUB_LOGINS` (the owner) give work; others get a short reply;
  - finds the PR's Linear issue: `Part of ENG-123` (or a closing keyword) in the PR body, then a
    Linear-style branch (`…/sha-123-…`), taking the first one Linear knows. A mention inside a quoted
    reply (`> …`) or in code doesn't count: it repeats an older ask;
  - **no linked issue** → a reply ("I only work on PRs linked to a Linear issue…") and no job (owner's
    decision, 2026-10-19);
  - otherwise opens an Autopilot session on the issue (`agentSessionCreateOnIssue`) and stores a
    `prompted` job with `source: github`, the PR and the comment URL; the prompt quotes the comment,
    and for a review comment its file, line and the end of its diff hunk. A second mention while that
    issue's job is still queued joins it;
  - acknowledges on both sides: a "Queued from a GitHub comment on <PR>" thought in Linear, and on
    GitHub 👀 on the comment plus a reply "Queued as ENG-123. I'll start within N minutes". Reviews
    have no reactions, so they get the reply only;
  - Linear or GitHub unreachable, or the job can't be stored: a reply saying so and no job (GitHub
    doesn't retry deliveries; the owner mentions again, and that same comment stays retryable). A
    session already opened for it gets an `error` activity. Redeliveries never queue twice.
- The Worker mints its own installation token (RS256 JWT from `GITHUB_APP_PRIVATE_KEY` →
  `/app/installations/<id from the webhook>/access_tokens`) and caches it in D1's `state` until
  5 minutes before it expires.
- The runner sees `job.source: github` and `job.github` (`repo`, `pr`, `commentUrl`) and answers on
  the PR when it finishes (orchestrator section 5).

## Secrets

| Where | Name | What |
|---|---|---|
| Worker secret + Keychain `autopilot-gateway/token` | `GATEWAY_TOKEN` | shared runner token (random, 256 bit) |
| Worker secret + Keychain `autopilot-gateway/webhook-secret` | `LINEAR_WEBHOOK_SECRET` | Linear's webhook signing secret |
| Worker secret (copied from Keychain `autopilot-linear`) | `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` | so the Worker can post the acknowledgement while the Mac sleeps |
| Keychain `autopilot-gateway/url` | — | the Worker's URL (not secret) |
| Worker secret (copied from Keychain `autopilot-github`) | `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET` | the GitHub App's key (base64 PKCS#8 DER) and webhook secret: `bin/autopilot-github-setup worker-secrets` |
| `wrangler.toml` vars | `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `ALLOWED_GITHUB_LOGINS` | not secret |

`bin/autopilot-gateway-setup` moves every value through pipes and `security -i` stdin only; nothing
is printed, put on a command line, or written to a file. Only the owner runs it.

## Deploy (once)

One Worker `autopilot-gateway` at `https://autopilot-gateway.<subdomain>.workers.dev` and one D1
database `autopilot-gateway`. Copy `gateway/wrangler.example.toml` to `gateway/wrangler.toml`
(git-ignored: it is your deployment) and fill in your account id, Linear and GitHub ids.

**Cloudflare auth: a scoped API token, never `wrangler login`.** `wrangler login` asks for a fixed
bundle of ~23 broad scopes (connectivity admin, billing read, ...). Instead the owner creates a
custom token (My Profile → API Tokens → Create Custom Token) with only:
- `Account` · `Workers Scripts` · `Edit`: deploy the Worker and set its secrets;
- `Account` · `D1` · `Edit`: create the database and apply migrations;

limited to their one account, and stores it in the Keychain:

```bash
security add-generic-password -U -s autopilot-cloudflare -a deploy-token -w
```

Every wrangler command reads it from there for that one process (`account_id` is in `wrangler.toml`):

```bash
cd gateway
W() { CLOUDFLARE_API_TOKEN="$(security find-generic-password -s autopilot-cloudflare -a deploy-token -w)" npx --yes wrangler@4.151.1 "$@"; }
W d1 create autopilot-gateway          # once; put its database_id in wrangler.toml
W d1 migrations apply autopilot-gateway --remote
W deploy
```

Then the owner, in Terminal from the repo root (the script uses the same Keychain token):

```bash
bin/autopilot-gateway-setup url https://autopilot-gateway.<subdomain>.workers.dev
bin/autopilot-gateway-setup runner-token
bin/autopilot-gateway-setup linear-client
```

And in Linear: Settings → API → Applications → **Autopilot** → edit.
- **Webhooks:** on.
- **Webhook URL:** `https://autopilot-gateway.<subdomain>.workers.dev/webhook`
- **Events:** tick **Agent session events** (nothing else is needed).
- Save, then copy the webhook's **signing secret** and run `bin/autopilot-gateway-setup webhook-secret`.

Check: `bin/autopilot-linear jobs next` prints `{"pending": 0, ...}`. Delegate a ticket in
**Autopilot Sandbox** to Autopilot: the "Queued" thought appears in its session within seconds.

From now on Linear allows agent sessions for the app, so the helper's "agent sessions disabled"
comment fallback no longer triggers.

GitHub App (0.5.0): `W d1 migrations apply autopilot-gateway --remote` (0002 adds the GitHub
columns), `W deploy`, then the owner runs `bin/autopilot-github-setup worker-secrets`. The app's
webhook URL (`<gateway>/github`) and secret come from its manifest; nothing to set by hand.

Redeploy after a change: `W deploy` (and `W d1 migrations apply autopilot-gateway --remote` when
`migrations/` changed). Rotate the runner token with `bin/autopilot-gateway-setup runner-token`.

## Runner side

`bin/autopilot-linear queue` uses the gateway as soon as Keychain `autopilot-gateway/url` exists
(`AUTOPILOT_GATEWAY=off` or `--source linear` polls Linear instead):
- `queue --count` is the gateway's `pending` (one HTTP call, no Linear call). If the gateway is down
  (network error or 5xx) it warns and polls Linear, so delegations still get worked; a 401 is an error;
- `queue --next` is the first job whose project isn't excluded, joined with its Linear issue, with
  `job` and `session.id` added. A job whose issue is gone is marked `blocked` and skipped;
- `jobs ack <id>` remembers the job in `~/.local/state/autopilot/current-jobs/<ISSUE>.json` (one per issue: runs work side by side). Until
  `jobs done|blocked <id>` (or for 6 h at most), `activity` and `link` for that issue post into the
  job's session.
- The token goes only to the Keychain URL (`AUTOPILOT_GATEWAY_URL` may only name localhost), and
  redirects are never followed.

Gotchas seen while setting it up (2026-10-19):
- Cloudflare's bot check answers **403** to Python's default `Python-urllib` user agent before the
  Worker runs; the helper sends `User-Agent: autopilot-runner/<version>`.
- Linear sent no webhooks at all until the app's **client secret was rotated** (which invalidates
  every client-credentials token); deliveries started right after. Re-authorizing the installed app
  isn't possible ("already installed"), and an authorization-code exchange is refused while
  client-credentials tokens exist ("Scope updates are not supported for client credentials tokens").
- Rotating the client secret also gave the webhook a **new signing secret**: re-run
  `bin/autopilot-gateway-setup linear-client` and `webhook-secret` after any rotation. A mismatch
  shows in the Worker log as `webhook rejected: bad signature`.

Delegations made while the webhook was off or failing have no job. `/autopilot:status` and
`queue --source linear` show them; re-delegate to queue one.

The scheduled task's prompt is unchanged.

## Cost: free tier, €0

The account is on the Workers **Free** plan (checked 2026-10-19: Workers plans shows Free as the
current plan; Billing → Subscriptions lists Workers Free and no Workers Paid). Free has no usage
billing: past the daily limits requests fail rather than cost money. The deploy token has no billing
permission, so it can't upgrade the plan. Use is far below them: one poll per 10 min is 144 requests a day, plus a
few webhooks, against 100,000 requests a day; D1 Free allows 5 M row reads and 100,000 row writes a
day and 5 GB, and the jobs table grows by a few rows a day.

## Local development

```bash
cd gateway && npm test                         # node --test, no dependencies (Node 22.13+)
npx --yes wrangler@4.151.1 dev                 # local Worker + local D1 on :8787
```

For `wrangler dev`, put test values (never real ones) in `gateway/.dev.vars`; it is gitignored.
