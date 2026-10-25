---
description: Pause Autopilot - scheduled runs exit immediately until /autopilot:resume
allowed-tools: Bash(mkdir *), Bash(touch *), Bash(date *)
---

Run `mkdir -p ~/.local/state/autopilot && date -u +%FT%TZ > ~/.local/state/autopilot/paused`.
Say: "Autopilot paused. A run already in progress finishes its current issue. /autopilot:resume to continue."
