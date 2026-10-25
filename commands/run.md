---
description: Run Autopilot once now (optionally for one delegated issue, e.g. /autopilot:run ENG-150)
argument-hint: "[ISSUE-ID]"
---

Run the Autopilot orchestrator now: invoke the `autopilot:orchestrator` skill and follow it.

If an issue ID was given ($ARGUMENTS), work that issue instead of `queue --next`: use the open
gateway job for it if `autopilot-linear queue` lists one (`jobs`, matched by `identifier`); otherwise
only if `autopilot-linear queue --source linear` lists it as delegated to Autopilot (in `issues` or
`held`), and then without a job. Otherwise say it isn't delegated to Autopilot and stop. Everything else (lock, pause flag, one issue, hard rules)
applies unchanged.
