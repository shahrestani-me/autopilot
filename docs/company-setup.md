# Set up Autopilot for your company

A step-by-step guide for the admin who sets Autopilot up for a company's Linear workspace and GitHub
organization. When you are done, anyone you allow can delegate a Linear issue to **Autopilot** and get
back a merged, preprod-verified PR, or one clear question in the issue.

Plan on about two hours for the first setup. Do the steps **in order**: each one produces something a
later one needs ([the setup chain](#the-setup-chain)). Every step ends with a **Check**; don't go on
until it passes. The reference pages have the details ([setup](setup.md), [gateway](gateway.md)).

## How it fits together

```
Linear issue ──delegate──▶ Gateway (your Cloudflare Worker) ──job──▶ Runner (your Mac)
                                                                     Claude Code runs the issue:
GitHub PR ◀──── branch, PR, review, merge as your GitHub App ◀─────── implement → PR → CI → merge → preprod check
```

- **One company, one deployment.** Autopilot is not a hosted service. Your company runs its own
  gateway, its own Linear app, its own GitHub App and its own runner. Nothing is shared with anyone else.
- **The runner is one always-on Mac** (a Mac mini or Mac Studio works well), signed in to an admin
  account. Every secret lives in that Mac's Keychain.
- **Preprod only.** Autopilot merges to your main branches and checks the result on preprod. Production
  releases stay with your team; the per-project config lists what it must never touch.

## The setup chain

Four pieces point at each other, so they are set up in this order:

```
1. Gateway Worker ──its URL──▶ Mac Keychain (autopilot-gateway/url)
                                  │
            ┌─────────────────────┴──────────────────────┐
            ▼                                            ▼
2. Linear app: its webhook → <URL>/webhook     3. GitHub App: its webhook → <URL>/github
   gives: the app's user id, people's ids         gives: the app's id and slug
            │                                            │
            └──────────────▶ gateway/wrangler.toml ◀─────┘
                             (who may give work, which app is ours)
                                  │
                                  ▼
                         redeploy the Worker
```

| Step | You create | It gives you | Who uses it |
|---|---|---|---|
| 1 | the gateway Worker | its URL `https://autopilot-gateway.<subdomain>.workers.dev` | the Mac (Keychain), Linear's webhook, the GitHub App's webhook |
| 2 | the Linear app | its user id, and the ids of the people allowed to give it work | `wrangler.toml` → redeploy |
| 3 | the GitHub App | its id and slug; the logins allowed to mention it | `wrangler.toml` → redeploy |
| 4 | project configs | which issues, repos, checks and preprod Autopilot works with | the runs |
| 5 | scheduled tasks | runs every few minutes | the Mac |

**The gateway comes first** because both webhooks point at its URL. **The Worker is deployed again** after
steps 2 and 3, because its settings (`gateway/wrangler.toml`) need the ids those steps produce. Until then it
deploys with placeholders and refuses all work, which is safe.

**If the gateway URL ever changes**, update all three places: the Keychain
(`bin/autopilot-gateway-setup url <new URL>`), Linear's webhook URL, and the GitHub App's webhook URL
(GitHub → your org → Settings → GitHub Apps → the app → Webhook URL).

## Before you start

You need:

| What | Why |
|---|---|
| A Mac (macOS 14 or later) that stays on and awake | it runs every ticket |
| [Claude Code](https://claude.com/claude-code) desktop app with a Claude subscription | runs are Claude Code sessions; scheduled tasks start them |
| Linear **workspace admin** | to create and install the Autopilot app |
| GitHub **organization owner** | to create the GitHub App and install it on repos |
| A Cloudflare account (the free plan is enough) | hosts the gateway Worker and its D1 database |
| An OpenRouter account (optional) | coding on non-Claude models, and API mode when the subscription runs out |

On the Mac, install and sign in to these tools first:

```bash
xcode-select --install          # git and python3
brew install gh node            # GitHub CLI, and node/npx for the Cloudflare CLI
gh auth login -h github.com     # an org admin; Autopilot reads PR state with this login
```

Then clone this repository. Every command below runs from this folder, and the clone keeps your
`gateway/wrangler.toml`, so keep it:

```bash
git clone https://github.com/shahrestani-me/autopilot.git ~/autopilot
cd ~/autopilot
```

### The deploy helper

Deploys use a Cloudflare token from the Keychain (step 1). Define this helper in every new Terminal
window before you run `W …`:

```bash
W() { (cd ~/autopilot/gateway && CLOUDFLARE_API_TOKEN="$(security find-generic-password -s autopilot-cloudflare -a deploy-token -w)" npx --yes wrangler@4.151.1 "$@"); }
```

## Step 1: Deploy the gateway (Cloudflare)

The gateway receives Linear's and GitHub's webhooks, answers "Queued" within seconds, and keeps the
job queue the Mac reads.

1. In Cloudflare, create a **custom API token** (My Profile → API Tokens → Create Custom Token) with only
   `Account · Workers Scripts · Edit` and `Account · D1 · Edit`, limited to your account. Store it
   (the command prompts for it, so it never lands in shell history):

   ```bash
   security add-generic-password -U -s autopilot-cloudflare -a deploy-token -w
   ```

2. Create your deployment file. It is git-ignored: it holds your account's ids, not code.

   ```bash
   cp gateway/wrangler.example.toml gateway/wrangler.toml
   ```

   Put your Cloudflare **account id** (dashboard → Workers & Pages, right-hand side) in `account_id`.
   Leave the Linear and GitHub values as placeholders; steps 2 and 3 fill them in.

3. Define [the deploy helper](#the-deploy-helper), then create the database, apply its schema and deploy:

   ```bash
   W d1 create autopilot-gateway                  # copy the printed database_id into gateway/wrangler.toml
   W d1 migrations apply autopilot-gateway --remote
   W deploy                                       # prints https://autopilot-gateway.<subdomain>.workers.dev
   ```

   Note the URL `W deploy` printed. The rest of the chain uses it.

4. Store the URL on the Mac, and create the runner token the Mac and the Worker share:

   ```bash
   bin/autopilot-gateway-setup url https://autopilot-gateway.<subdomain>.workers.dev
   bin/autopilot-gateway-setup runner-token
   ```

**Check:** `bin/autopilot-linear jobs next` prints `{"pending": 0, ...}`: the Mac reaches the gateway
with its token. (Opening the URL in a browser shows a 404; the Worker only answers its API paths.)

Details: [gateway.md](gateway.md#deploy-once).

## Step 2: Create the Linear app

1. Follow [setup.md, section 1](setup.md#1-create-the-linear-app-workspace-admin-5-min): create the app
   (name **Autopilot**, client credentials on, webhooks off for now), store its client id and secret in
   the Keychain, install it into the workspace as an agent, and give it access to every team it should
   work in.

2. Get the ids the gateway needs:

   ```bash
   bin/autopilot-linear auth-check      # "ok": true; viewer.id is the app's own user id
   bin/autopilot-linear users           # everyone's ids
   ```

3. In `gateway/wrangler.toml` set:
   - `APP_USER_ID`: the app's user id (`viewer.id` above);
   - `ALLOWED_USER_IDS`: the user ids of everyone who may give Autopilot work, comma-separated. Others
     who delegate or mention it get a polite refusal and no job.

4. Send the Linear client to the Worker, then turn on Linear's webhook. In Linear: Settings → API →
   Applications → **Autopilot** → Webhooks **on**, URL `<gateway URL>/webhook` (the URL from step 1),
   event **Agent session events**. Save, copy the signing secret, and store it:

   ```bash
   bin/autopilot-gateway-setup linear-client
   bin/autopilot-gateway-setup webhook-secret     # prompts for the signing secret
   ```

5. Redeploy, so the gateway knows the app and the allowed people:

   ```bash
   W deploy
   ```

6. **Turn off Linear's PR automations** in each team Autopilot works in (team settings → Workflows &
   automations → set every PR event to *No action*). Autopilot sets every status itself; this lists any
   that would fight it:

   ```bash
   bin/autopilot-linear automations --team <TEAM KEY>
   ```

**Check:** create the small test issue from [step 7](#step-7-try-it) now, in the Linear project you will
configure in step 4, and delegate it to **Autopilot**. Within seconds its agent session shows *Queued*, and `bin/autopilot-linear jobs next` shows
the job. It waits there: nothing runs it until step 5.

## Step 3: Create the GitHub App

Autopilot's branches, commits, PRs and merges show as your own GitHub App's bot, not as a person.

1. Create the app on your organization. GitHub app names are global, so choose your own:

   ```bash
   AUTOPILOT_GITHUB_ORG=<your-org> bin/autopilot-github-setup create-app --name <your-org>-autopilot
   ```

   A local page opens; review the permissions on GitHub and click **Create GitHub App**. The script stores
   the app's id, slug, private key and webhook secret in the Keychain. The app's webhook URL is
   `<gateway URL>/github`, read from the Keychain (step 1).

2. Install it: `https://github.com/apps/<slug>/installations/new` → your org → *Only select repositories*
   → the repos Autopilot may work in. Add a repo here before a project config uses it.

3. Copy its key and webhook secret to the Worker:

   ```bash
   bin/autopilot-github-setup worker-secrets
   ```

4. In `gateway/wrangler.toml` set:
   - `GITHUB_APP_ID` and `GITHUB_APP_SLUG`: print them with

     ```bash
     security find-generic-password -s autopilot-github -a app-id -w
     security find-generic-password -s autopilot-github -a app-slug -w
     ```
   - `ALLOWED_GITHUB_LOGINS`: the GitHub logins who may give Autopilot work with an `@<slug>` mention on
     its PRs, comma-separated.

5. Redeploy: `W deploy`.

6. **Branch protection.** Keep your required checks. If a ruleset uses
   `require_extra_approval_for_unattributed_changes`, it treats the bot's commits as needing an extra
   approval; turn that flag off for repos Autopilot merges in, or a person approves each PR
   ([why](setup.md#github-app-autopilots-github-identity-since-050)).

**Check:** `bin/autopilot-github auth-check` prints `"ok": true`, the bot (`<slug>[bot]`) and the
repos it is installed on. In GitHub → your org → Settings → GitHub Apps → the app → Advanced →
Recent Deliveries, open the `ping` and press **Redeliver**: it now gets a 200. (The first delivery, sent
before the webhook secret reached the Worker, shows an error; that's expected.)

## Step 4: Install the plugin and describe your projects

1. Install the plugin in Claude Code:

   ```bash
   claude plugin marketplace add shahrestani-me/autopilot
   claude plugin install autopilot@autopilot
   ```

2. Write one config per project, outside this repo:

   ```bash
   mkdir -p ~/.config/autopilot/projects
   cp examples/web-app/autopilot.yaml ~/.config/autopilot/projects/<project>.yaml
   ```

   Edit it ([schema](../schema/autopilot.schema.json)). The parts that matter most:

   | Section | Set |
   |---|---|
   | `linear` | the team key, the Linear projects Autopilot takes issues from, your workflow state names, the owner who answers questions |
   | `repos` | each repo's GitHub name, local clone path, setup and check commands, house rules and notes |
   | `merge` | merge policy, review depth, review model |
   | `preprod` | the preprod URL, test users, and how to verify a change in a browser; optionally `sign_in`, so runs sign a test user back in with Passhand ([setup](setup.md#5-preprod-sign-in-with-passhand-optional-since-0170)), and `browser_host`, a Chromium of Autopilot's own for drivers that connect over CDP, such as Playwright MCP ([setup](setup.md#6-browser-host-for-cdp-drivers-optional-since-0180)) |
   | `forbidden` | production hosts, cloud projects, commands and workflows it must never touch |
   | `spend` | the most a ticket may spend on paid models |

   Name the file after the config's `name`. Clone each repo to the `path` you gave it.

**Check:** in Claude Code, `/autopilot:status` shows the Linear identity, the GitHub App, the queue and
your project files, with no errors.

## Step 5: Schedule the runs

In the Claude Code desktop app, create three scheduled tasks named `autopilot-agent`,
`autopilot-agent-2` and `autopilot-agent-3`, every 5–10 minutes, in Auto mode, with the prompt from
[setup.md, section 4](setup.md#4-schedule-it). Each run takes one issue, so up to three issues are worked
side by side. Idle runs end after one quick check and cost almost nothing.

**Check:** run one task by hand (or `/autopilot:run`). With an empty queue it ends at once.

## Step 6 (optional): Non-Claude models and API mode

Add an OpenRouter key with a credit limit to code individual tickets on another model, or to keep working
when the Claude subscription hits its limit ([setup](setup.md#openrouter-coding-on-non-claude-models-since-0140-optional),
[API mode](setup.md#api-mode)):

```bash
security add-generic-password -U -s autopilot-openrouter -a api-key -w
~/.claude/plugins/marketplaces/autopilot/bin/autopilot-fallback install-tick
```

**Check:** `bin/autopilot-coder check` prints `"ok": true`.

## Step 7: Try it

1. Use the small, low-risk Linear issue you delegated in step 2 (for example "Fix the typo in the README
   heading"), in one of the configured projects. If you skipped that check, create and delegate it now:
   within seconds its agent session shows *Queued*.
2. Within one schedule interval a run picks it up. Follow it live in the issue's Autopilot session:
   it opens a PR as your app, waits for CI and review, merges, checks preprod, and marks the issue Done.

If nothing happens, see [Getting help](#getting-help).

## Day to day

| Who | Does |
|---|---|
| Anyone in `ALLOWED_USER_IDS` | delegates issues to Autopilot, answers its questions in the issue |
| Anyone in `ALLOWED_GITHUB_LOGINS` | asks for changes on its PRs with `@<slug> …` |
| The admin | `/autopilot:pause` and `/autopilot:resume`, adds projects and repos, keeps the Mac on |

To add a person: add their Linear id (`bin/autopilot-linear users`) to `ALLOWED_USER_IDS` in
`gateway/wrangler.toml` and run `W deploy`. To add a repo: install the GitHub App on it, then add it to a
project config.

## Getting help

| Symptom | Look at |
|---|---|
| No *Queued* in Linear after delegating | Linear's webhook URL and event (step 2.4), `ALLOWED_USER_IDS` and the redeploy (steps 2.3, 2.5) |
| *Queued*, but no run ever starts | the scheduled tasks (step 5), `/autopilot:status` for `paused` or a gateway error |
| `@<slug>` on a PR does nothing | the GitHub App's webhook deliveries (step 3 check), `ALLOWED_GITHUB_LOGINS` and the redeploy |
| A helper exits with code 2 | a Keychain item is missing: the message names it ([list](setup.md#keychain-items)) |
| Anything else | [Troubleshooting](../README.md#troubleshooting), and `/autopilot:status` |

Still stuck? Open an issue on this repository with what `/autopilot:status` prints (it never shows
secrets).

## Security checklist

- Secrets only in the Mac's Keychain and as Cloudflare Worker secrets; never in files, chat or issues.
- `gateway/wrangler.toml` and `~/.config/autopilot/` stay out of git.
- The GitHub App is installed on selected repositories only.
- Every project config lists its production hosts, cloud projects and release commands under `forbidden`.
- The Mac's user account is the trust boundary: anyone who can use it can act as Autopilot.
- Passhand logins marked `auto` (for `preprod.sign_in`) are preprod test accounts only, never a personal
  or production account.
- With `preprod.browser_host`, the browser host's CDP port (`127.0.0.1:9333` by default) gives anything
  running as you on the Mac full control of that browser, signed in as its test user. Give it a preprod
  test user of its own, and stop it (`bin/autopilot-browser stop`) when it isn't needed.
