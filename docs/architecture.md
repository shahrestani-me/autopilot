# Architecture

Source of truth for the design. It was first drafted as a Linear document,
and is kept here per D3.

## Goal

Linear is the only interface. Delegate an issue to **Autopilot**, or (from phase 2) @mention it, and it:

1. picks the issue up;
2. implements it on a branch with Claude Code;
3. opens a PR, gets CI and AI review green, and merges;
4. verifies on the project's preprod;
5. replies in the issue **as Autopilot**, marking it Done or asking one clear question.

The owner answers in the issue. Linear's own notifications reach the phone.

**Non-goals:** prod access while unattended; new paid model spend (it runs on the Claude
subscription; the exceptions go through OpenRouter under spend caps: coding on a non-Claude model an
issue asks for, and API mode when the subscription is out or the owner turns it on); multi-tenant
SaaS (one owner, their own Mac).

## Components

```
Linear ──webhook──▶ Gateway (phase 2)  ──queue──▶ Runner (Mac Studio)
   ▲   AgentSession   Cloudflare Worker + D1      Claude Code scheduled task:
   │   events         acks in <10 s, stores jobs  one session per issue,
   │                                              per-project autopilot.yaml
   └──── comments, activities, status as "Autopilot" (app token from Keychain) ◀──┘
```

1. **Identity:** a Linear OAuth app "Autopilot", installed with `actor=app` and the scopes
   `read, write, app:assignable, app:mentionable`. Agents take no billable seat. The owner stays
   **assignee**; Autopilot is the **delegate**. See [setup.md](setup.md).
2. **Gateway (phase 2, D1 = Cloudflare Worker + D1):** verifies the webhook signature, posts an
   immediate "Queued" activity, stores the job, and exposes `GET /jobs/next` and
   `POST /jobs/:id/ack|done|blocked` for the runner behind a shared token (also in Keychain).
   See [gateway.md](gateway.md).
3. **Runner:** this repo, a Claude Code plugin (D2):
   - `skills/orchestrator`: gate, lock, intake, claim, outcome, report;
   - `skills/worker`: one issue end to end, driven by the project's `autopilot.yaml`;
   - `bin/autopilot-linear`: the only path to Linear for runs, so every write is Autopilot's;
   - `commands/`: `/autopilot:status`, `/autopilot:run [ID]`, `/autopilot:pause`, `/autopilot:resume`, `/autopilot:api-mode`;
   - hooks (phase 4) that *enforce* the hard rules: prod hosts and projects, `gh release`, pushes to
     the default branch, direct DB writes, secret reads.

## Phase 2 intake (gateway jobs)

Once the gateway is set up, `autopilot-linear queue` reads jobs from it instead of polling Linear:

- Linear sends an `AgentSessionEvent` for each delegation, @mention and reply in an Autopilot
  session. The gateway stores a job and posts "Queued; I'll start within N minutes" in that session
  within seconds, whether or not the Mac is awake.
- Each run takes one job (D5): an interrupted one first, then the oldest. It `ack`s it, posts into
  the job's session, and ends it with `done` or `blocked`.
- **Mentions and replies resume blocked work:** an elicitation leaves the session awaiting input; the
  owner's reply in that thread, or a new @mention, is a new job, and the run reads the whole thread
  (all Autopilot sessions on the issue) as context.
- A stop in Linear cancels the session's queued jobs.

Phase 1 polling stays as the fallback (`AUTOPILOT_GATEWAY=off`, or no gateway in the Keychain).

## Phase 1 intake (polling, no gateway)

Each scheduled run calls `autopilot-linear queue`:

- the filter is `delegate = Autopilot`, with the state not completed or canceled, optionally narrowed
  to a project or team;
- **held back:**
  - issues whose latest Autopilot agent session is `awaitingInput` or `error` and has no human comment
    since (the owner hasn't answered yet);
  - issues with open sub-issues;
- **order:** issues already started (an interrupted run) first, then priority Urgent → Low → None,
  then the oldest.

The run takes the first one only (D5). It claims it with a `thought` activity, sets the
in-progress state and posts a comment, all as Autopilot.

A block becomes an `elicitation` activity: the session turns *awaiting input* and Linear notifies the
owner. Their reply is a human comment newer than the session's last update, so the next poll
takes the issue again.

**Agent sessions need webhooks.** Linear only allows agent sessions for an app with webhooks on and
**Agent session events** selected (`agentSessionCreateOnIssue` fails with "agent sessions disabled"
otherwise). That needs the phase 2 gateway URL; once it is set, these fallbacks no longer trigger.
Without it the helper falls back:
- `elicitation` / `error` / `response` activities become comments, posted as Autopilot. Elicitation and
  error comments carry a 🔴/⚠️ marker, and `queue` holds an issue whose newest Autopilot comment has one
  until a human replies;
- `thought` / `action` activities and session links are skipped.

Activities need an agent session. Linear creates one on delegation when webhooks are on; in
phase 1 the helper creates one with `agentSessionCreateOnIssue` when none is open.

## Decisions (2026-10-19)

| | Decision |
|---|---|
| D1 | Gateway on **Cloudflare Worker + D1** (phase 2). Phase 1 polls. |
| D2 | A **Claude Code plugin**, in this repo. |
| D3 | **Self-contained.** Autopilot's code *and* its knowledge (design, decisions, lessons) live together in this repo. |
| D4 | **Merge everything** green and AI-reviewed (preprod only). |
| D5 | **Strictly one issue at a time**, across all projects. |

## Phases

1. **Identity + runner polling** (ENG-141): app, Keychain, plugin skeleton, delegate intake, posting as Autopilot.
2. **Gateway** (ENG-142): instant acks, a job queue, mentions.
3. **First real project migration** (ENG-143), then retire the project's old hand-written loop.
4. **Hooks hardening** (ENG-144): enforced no-prod, spend caps, secret-read guard.
5. **Second project** (ENG-145), to prove the config is generic.
