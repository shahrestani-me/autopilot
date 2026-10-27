# Lessons learned

From running Autopilot, and the hand-written loop before it, on real projects (since 2026-10-18).
Each one is already built into the skills; this page says *why*, so nobody undoes them.

## Linear

- **Linear's PR automations move the issue by themselves** (team settings → Workflows & automations:
  PR open → In Progress, review → In Review, merge → Done). They fire for every PR *linked* to the
  issue, and `Part of <ID>` in the PR body links it too: keeping the ID out of the branch and title
  (`git.ticket_id_in_branch_or_title: false`) is not enough. On 2026-10-21 they marked two
  issues Done on their first merge while the run was still opening the next PRs. Autopilot
  sets every status itself, so the team's automations must be "No action". Preflight warns when they
  aren't (`autopilot-linear automations`), and the worker sets its status with `--settle`, which puts
  it back if Linear changed it a few seconds after a merge.
- **A session the app opens itself has no creator** (`creator: null`), so the gateway can't tell it
  from a Linear automation's. On 2026-10-21 a run on one issue found its old session stale and opened a
  new one; the gateway answered it "Sorry, I only take work from my owner here", which closes it. The
  stream hook had also lost the session it opened from its state, so every post opened another: four
  sessions in 30 s, and the "Run by Agent 1" line landed in the first, closed one. Since 0.13.1 the runner
  announces a session before opening it (`POST /sessions/expect`) and the hook keeps it for the run.
- A bare `#123` in Linear text links to the wrong repo. Write full PR URLs.
- Posting through the owner's Linear MCP makes every update look like the owner wrote it, which is
  the reason this identity exists. Runs write only through `bin/autopilot-linear`.
- Auto-promoting Backlog tickets when Todo is empty was the owner's ask for the old loop. With
  delegation, the owner chooses explicitly; follow-ups the agent files go to Backlog and are never
  delegated to it.

## Scheduling and cost

- **One issue per session.** The owner wants each issue's transcript as its own sidebar row. Scheduled
  runs can't start other sessions, so throughput is one issue per run.
- **The idle cost matters.** The scheduled prompt fast-exits in one Bash call (pause flag, lock,
  `queue --count`) before any skill loads.
- A 30-minute cadence looked "paused" to the owner with 9 tickets queued; 10 minutes was right.
- Keep the Mac awake with `caffeinate -is -t 1500` while work is queued; the Mac Studio's own sleep is off.
- A stale lock (over 6 h) means a crashed run: overwrite it and say so in the report.

## Notifications

- An unattended session's `PushNotification` is "not sent" (Remote Control is off), and supervisor
  completion notices didn't arrive either. Linear notifications from the Autopilot identity replace
  all of that: the elicitation *is* the notification.

## Browser verification

- **Never read `localStorage.auth_token` or cookies to call the API from page JS.** Auto mode flagged
  it as data exfiltration and then denied even plain clicks for the rest of the session. Verify via the
  UI, `read_network_requests`, or the app's own AI chat.
- Never type passwords, not even test users'. The owner signs in once with "Keep me logged in"; a
  daily keep-alive visit refreshes the session. Signing out kills the only session.
- A session that can't refresh still ends with its token, so a weekly sign-in by the owner blocked
  verification every time. Since 0.17.0 a project can opt in to `preprod.sign_in`: Passhand fills the
  test user's saved login in Chrome and Autopilot only clicks Sign in, never seeing the password. One
  attempt per 20 hours, because repeated failed sign-ins can lock the account.
- A CDP driver can't attach to the owner's Chrome: Chrome refuses remote debugging on its default
  profile. A separate Chromium with its own profile works, with Passhand loaded through the profile's
  `NativeMessagingHosts/` folder. Killing a Node launcher left that Chromium running, so the browser
  host (0.18.0) starts Chromium itself and `stop` ends its whole process tree. Limiting hosts in the
  browser's resolver blocks every other host, IP addresses included, whatever the driver asks for.
- One test user's sessions in two browsers can end each other (a sign-out may revoke them all, an app
  may cap live sessions per user), so a second browser gets a test user of its own, or one persistent
  session that never signs out.
- Chrome holds one session per site, so two-user flows are verified on one side, then handed to the
  owner as an exact incognito check.
- The Chrome extension shows a 204 as 503 in `read_network_requests`; check the page's Resource
  Timing `responseStatus` before calling it a failure.
- Prefix test data with `[autopilot]` and delete it afterwards.

## Git and CI

- Worktrees only; never touch the shared checkouts, which the owner's other sessions use.
  Parallel sessions move HEAD, so check `git branch --show-current` before every commit and push.
- `.gitignore`'s `node_modules/` doesn't match a **symlink** named `node_modules`. `git add -A` in a
  worktree commits it. Add files by name.
- Wait for **every** CI check, not just the required one.
- Mutation-test new tests: revert the production line and watch the test fail.
- Merging to main deploys preprod. If a deploy breaks, set the preprod-broken flag so later runs stop
  taking work until both deploys are green again.

## Spend

- The live eval gate costs about $1.30–1.70 a run: never run it unattended. Keep paid model spend at or
  under $1 per issue and log every paid action with its estimate.

## What the migration from the old loop dropped on purpose

- **Moving tickets to Todo, the `autopilot` / `autopilot-blocked` labels and the Backlog refill.**
  Delegation is the intake, an elicitation is the block, and the owner's reply is the resume.
- **Push notifications and the `NOTIFY:` line.** Linear notifies the owner's phone about elicitations
  and about the keep-alive's sign-in notice (an issue assigned to `linear.owner`).
- **The old loop's board artifact and the per-ticket Linear project update.** The agent session
  view shows each issue's progress; a live console replaces the board later. The board's
  copyable fixes live on as `preprod.known_fixes`, quoted in elicitations.
- **The second lock.** During the hand-over the old loop took the plugin's `run.lock`, so the two
  loops could never run at once.

## Done means nothing is left for Autopilot (owner, 2026-10-19)

An issue is **Done** once it is merged and verified on preprod. It never sits in In Review waiting for a
prod release, nor for a check only the owner can do (device QA, a second user, an admin): that check goes
to the release project (`linear.release_checks_project`), which the owner
works through before a prod release.

## An owner's draft is a hold (2026-10-19)

The owner ran `/code-review` on an API PR, posted the findings with `@autopilot`,
and converted the PR to draft to hold the merge. The run already working the issue never re-read the
thread: it ran `gh pr ready` and merged 20 s later. The mention only queued a new job for later.
Since 0.4.1 the worker treats a draft as the owner's hold, and re-reads owner comments right before
merging.

## Autopilot is a GitHub App (0.5.0, 2026-10-19)

After the PR 510 miss the owner wanted to talk to Autopilot on the PR itself, and to see its PRs as
its own. Autopilot is now the private GitHub App `claude-autopilot`: PRs, commits, comments and merges
show as `claude-autopilot[bot]`, and an `@claude-autopilot` mention on a PR is queued like a Linear
mention, on the PR's Linear issue. Things learned:
- What really forced `--admin` was not the approval count (0 on both rulesets) but the preview flag
  `require_extra_approval_for_unattributed_changes`: bot- and AI-authored commits count as
  "unattributed" and need an approval, which an author can't give. Turned off on both rulesets; the
  ruleset API resets it to true when an update omits it.
- An app can't `--admin`. A missing approval now blocks and asks the owner, who can approve the
  app's PRs.
- The private key never touches disk: the manifest conversion's PEM goes through `openssl pkcs8` on
  pipes into the Keychain, and the helper signs the JWT in pure Python (RS256 with CRT plus a verify
  self-check), with no `cryptography` dependency.
- git hands a working credential to every configured helper to store. The wrapper resets the helper
  list and answers from the environment, so osxkeychain never stores an installation token.
- Installation tokens are no longer `ghs_` + 36 alphanumerics: they are ~380 characters with `.`
  and `-` (JWT-like). The first `auth-check` refused one; the helper now accepts `ghs_[A-Za-z0-9_.-]+`.
- New PR comments and reviews by a human are a merge hold too (the worker re-reads them right before
  merging), whether or not they mention the app.


## Finish before start, and listen first (owner, 2026-10-20)

On 2026-10-19 four green Autopilot PRs, for two issues, sat unmerged for hours. Both had blocked on a
review workflow that refused the new GitHub App; the owner fixed the workflow at 22:42, but a blocked
job only came back on an owner reply, and the queue was strictly oldest-first, so the runs moved on to
other tickets. The owner's replies and PR mentions also waited behind new tickets, and a finished run's
session that got a follow-up message kept writing without the lock, overlapping the next run.

Since 0.6.0: one ticket in flight (any open Autopilot PR). Each run sweeps in-flight work first
(`bin/autopilot-inflight`): finish green PRs, rebase conflicts, fix red checks, and take a new ticket
only when nothing is open. A block records what it waits for; `checkable` blocks resume by themselves
once PR heads, base branches or checks change, `owner` blocks push-notify and wait for the reply. The
gateway orders the owner's messages (`prompted`) ahead of new work (`created`), and the worker reads
messages about its own issue at every checkpoint. The gateway also ignores the "created" event of a
session it opened itself for a PR mention (it used to answer "I only take work from my owner").
The review of that change added: the scheduled fast exit also counts in-flight work (`autopilot-inflight
count`), or the sweep would never run with an empty queue; automatic resumes are capped at 2 per issue
(`claim`, reset only by `clear`), then it's reported stalled; checks pending for 2 h are stalled too, and
each stall push-notifies once.

## One session per issue (2026-10-20)

The owner delegated one issue twice within two minutes, so the gateway opened two Linear sessions with a job
each. The run acked both, and the helper made the *last* ack the run's session: the session the owner
opened showed only "Queued" while the work went to the other. Since 0.6.1 the gateway joins a second
delegation or mention to the issue's open job (and PR mentions to the running session), and the helper keeps
the first acked job as the run's session, posting a pointer and the final word into any other.

## Side by side (owner, 2026-10-20)

Up to three runs work at once (`AUTOPILOT_MAX_RUNS`), replacing D5's one-at-a-time. Each run holds a
slot (`autopilot-inflight slot`), and the issue it works on is locked (`lock <ID>`), so no two runs ever
touch one issue; the sweep marks locked issues `claimed`. Capacity counts issues with open PRs plus issues
a run holds. A new ticket starts only if it's independent: nothing open blocks it in Linear, no parent/child
or blocks tie to in-flight work, and (checked by the worker after planning, and again before merging) no
other in-flight PR touches the same files; otherwise it waits (`wait --kind slot --after <IDs>`). The
browser and the test user are shared, so preprod verification and the keep-alive take the `verify` lock.
Run state (`current-jobs/<ISSUE>.json`) is per issue; a single `current-job.json` would be overwritten.
Review fixes: locks and slots carry an owner token (only the holder touches or releases them), go stale
after 2 h untouched (runs touch slot and issue lock between phases), and a stale one is taken over by
one run at a time (`.takeover` marker). Waits for overlapping work are RESULT `wait: slot` + `after`,
which never push-notify the owner.

## The Linear session is the run (owner, 2026-10-20)

The owner wanted the Linear agent session to show the whole run, like opening the scheduled run's session
in Claude, so there's one place to watch and steer. Since 0.8.0 a plugin hook (`bin/autopilot-stream`,
PostToolUse and Stop) mirrors every message as a `thought` and every tool call as an `action` into the run's
session on its issue. It starts when a Bash call prints `autopilot-inflight`'s `{"locked": true, "name":
"<ID>"}` (detected from the output: runs call the helper through "$AI"), stops on unlock or at the end of
the run, skips the helpers' own Linear posts, redacts secrets and never fails the run
(`$STATE/stream.log`). Other sessions pay ~30 ms per tool call and nothing else. `AUTOPILOT_STREAM=0`
turns it off.

## Merging next to other writers

- **A green, reviewed PR can still delete someone else's work.** On 2026-09-14 an interactive session
  rewrote a CI-only gitleaks PR on top of a newer main from a working copy
  that still held the old files. Its squash deleted all of the PR merged before it (1,921 lines, 6 test files)
  57 minutes after that one merged; a PR in a second repo did the same in the same minute.
  CI stayed green because the tests went with the code, and nobody noticed for five weeks.
  `bin/autopilot-clobber check` (the worker's base guard) now refuses any diff that deletes base lines
  newer than the PR; `audit` replays the test over history (2026-10-21: those two were the only real
  hits across 675 merged PRs, both since restored).
- **Two green PRs are not a green pair.** Two PRs to one API repo each
  passed CI against their own base and were merged 5 s apart; together they failed the preprod
  deploy's tests. The base guard also requires the merged head to contain the current base.
