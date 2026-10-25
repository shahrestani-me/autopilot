---
description: Show Autopilot's state - identity, queue, lock, pause flag, last report
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/autopilot-linear *), Bash(${CLAUDE_PLUGIN_ROOT}/bin/autopilot-fallback status), Bash(${CLAUDE_PLUGIN_ROOT}/bin/autopilot-github auth-check), Bash(ls *), Bash(cat *), Bash(tail *), Bash(stat *), Bash(test *)
---

Report Autopilot's state in at most 10 lines. Run these (never print Keychain secrets):

1. `${CLAUDE_PLUGIN_ROOT}/bin/autopilot-linear auth-check`: who the token acts as. Exit 2 means the Keychain setup is missing: point to `docs/setup.md` in the plugin.
2. `${CLAUDE_PLUGIN_ROOT}/bin/autopilot-github auth-check`: the GitHub identity. `ok` → the bot and the repos the app is
   installed on; exit 2 → no app on this Mac, runs use the owner's `gh` login; exit 1 → the app is set up but failing,
   so runs on repo projects stop until it's fixed (report the error).
3. `${CLAUDE_PLUGIN_ROOT}/bin/autopilot-linear queue`: with the gateway, its open jobs (`jobs`: issue, kind, status,
   attempts) and `pending`; otherwise takeable issues (`issues`) and held ones (`held`, with `heldBecause`).
   A gateway error here (unreachable, 401) is worth reporting: runs can't take work until it's fixed.
4. `ls ~/.config/autopilot/projects/`: configured projects.
5. Flags in `~/.local/state/autopilot/`: `paused`, `auth-broken` and `github-broken` (show their text), `slots/*.lock` (busy run slots, with their age) and `locks/*.lock` (issues runs hold, plus `verify`/`keepalive`), `*.preprod-broken`, `.queue-empty`, `*.keepalive` (age), `*.signin-notice` (the open notice's ID), `api-mode` and `claude-limited` (API mode: also run `${CLAUDE_PLUGIN_ROOT}/bin/autopilot-fallback status` and show why, the models and today's spend), `linear-automations` (Linear's PR automations are on and move issues by themselves: show the teams and events, and the fix) and `linear-automations.notice`, and `attempts/*` above 0, and `current-jobs/*.json` (jobs runs claimed and didn't finish).
6. The last section of today's report, if any (`reports/` under each config's `reports.dir`, default `~/.local/state/autopilot/reports/`).
