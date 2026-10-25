---
description: API mode - run Autopilot on OpenRouter models instead of Claude (on, off, set KEY VALUE, or status)
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/autopilot-fallback *)
---

The argument is `on`, `off`, `set <key> <value>`, or empty (status). With `F=${CLAUDE_PLUGIN_ROOT}/bin/autopilot-fallback`:

- `on` / `off`: run `$F mode <arg>`.
- `set <key> <value>`: run `$F config set <key> <value>` (keys: orchestrator, reviewer, coder,
  usd_per_ticket, usd_per_day, timeout_min, permission_mode; it refuses values out of bounds).
- empty: run `$F status`.

Report in at most 6 lines: whether API mode is active and why (switched on, or a Claude usage limit),
the three models, today's OpenRouter spend against `usd_per_day` (Autopilot's own ledger), whether a
run is going, and whether the launchd tick is installed (if not, give the `install-tick` command from
`docs/setup.md#api-mode`). Exit 2 means no OpenRouter key: point to that section. After `off`, say
that a Claude usage limit still switches it on by itself until Claude works again.
