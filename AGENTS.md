# AGENTS.md

Rules for working on this repository, for people and agents alike.

## Public repository

This repository is public. Anyone can read it, and every commit, PR, issue and comment stays visible.

- **No private data.** No real hostnames, URLs or domains of a deployment, no company, product or
  repository names of the projects Autopilot works on, no test user names, e-mail addresses, Linear
  or GitHub ids, issue keys or PR numbers from those projects, no local paths with a user name, and
  never a secret, token or password. Use placeholders: `example.com`, `staging.example.com`,
  `test-user`, `ENG-123`, `~/…` or `/Users/me/…`. This holds for code, docs, examples, tests, commit
  messages and PR descriptions.
- **Generic only.** Autopilot is for anyone's projects. Don't add behaviour, defaults or rules for one
  particular project. What differs per project goes in its config (`preprod.recipes`,
  `preprod.known_fixes`, `repos[].notes`, …), which lives in `~/.config/autopilot/projects/` and
  never here. A new capability is an option in `schema/autopilot.schema.json`, off unless a config
  turns it on.
- **Lessons stay general.** `docs/lessons-learned.md` records what was learned, not where: say "an
  app whose sessions can't refresh", not which app.

`tests/test_public_repo.py` catches the common slips (real e-mail addresses, home-folder paths); the
rest is up to whoever writes the change. Before a commit, read your diff for names that only make
sense in one deployment.

## Layout, build and test

See [README.md, Repository](README.md#repository) and [Tests](README.md#tests).
