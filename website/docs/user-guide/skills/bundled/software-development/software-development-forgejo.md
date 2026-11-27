---
title: "Forgejo — Private Git on a self-hosted Forgejo or Gitea forge"
sidebar_label: "Forgejo"
description: "Private Git on a self-hosted Forgejo or Gitea forge"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Forgejo

Private Git on a self-hosted Forgejo or Gitea forge.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/software-development/forgejo` |
| Version | `1.0.0` |
| Author | Wizard (Wizard815), Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `forgejo`, `gitea`, `git`, `self-hosted`, `mirrors`, `pull-requests`, `private-repos` |
| Related skills | [`github`](../../bundled/software-development/software-development-github.md), [`codebase-inspection`](../../bundled/software-development/software-development-codebase-inspection.md) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Forgejo Skill

Work with repos on the user's own Forgejo or Gitea forge: create private repos,
push with the forge token, open and merge PRs, file issues, read CI runs, and
set up mirrors to and from GitHub. GitHub work stays with the `github` skill;
this skill decides which one a repo belongs to.

## When to Use

- A repo's `origin` is on the forge (check with `where`, below).
- The user asks for a private repo, or for work kept off GitHub.
- Creating a new repo and `forgejo.default_home` is `forge`.
- Mirroring a forge repo to GitHub, or a GitHub repo onto the forge.

## Prerequisites

- `FORGE_TOKEN` in `~/.hermes/.env` (prompted when this skill loads).
- `skills.config.forgejo.url` set (`hermes config set skills.config.forgejo.url http://{unraid-ip}:3030`).
- For GitHub mirrors: `GITHUB_TOKEN` with write access to the target repo.
- Optional: `FORGE_HOSTS` (comma separated) for other addresses of the same
  forge, such as `forgejo:3000` on the Docker network, so `where` knows them.
- Python 3 and `git` on the `terminal` path. No `gh`, `tea` or other CLI needed.

## How to Run

Everything goes through one helper (path relative to this skill), with the
configured address passed as `--url`:

```
python3 scripts/forge.py --url {forgejo.url} COMMAND ...
```

It prints JSON, exits 1 with `error: ...` on failure, and reads the token from
the environment only. Never put the token on a command line or in a remote URL.

## Quick Reference

| Task | Command |
|---|---|
| Which forge is this remote on? | `where "$(git remote get-url origin)"` |
| Check the token | `whoami` |
| New private repo | `repo create NAME [--org ORG] [--description TEXT]` |
| Push / fetch / clone with the token | `git -- push origin BRANCH` (any git arguments after `--`) |
| Open a PR | `pr create OWNER/REPO --head BRANCH --base main --title T --body-file F` |
| List, view, merge PRs | `pr list OWNER/REPO`, `pr view OWNER/REPO N`, `pr merge OWNER/REPO N --style merge` |
| Issues | `issue create OWNER/REPO --title T --body-file F`, `issue list OWNER/REPO` |
| CI (Forgejo Actions) runs | `runs OWNER/REPO --limit 5` |
| Mirror forge repo to GitHub | `mirror push OWNER/REPO --to https://github.com/OWNER/REPO` |
| Copy a GitHub repo onto the forge | `mirror pull https://github.com/OWNER/REPO [--name N]` |
| Show or run a repo's mirrors | `mirror list OWNER/REPO`, `mirror sync OWNER/REPO` |

## Procedure

1. **Decide the forge.** Run `where` on `origin`. `github` → use the `github`
   skill. `forge` → this skill. No repo yet → a `forge:` line in the project's
   `AGENTS.md` or `HERMES.md` wins (`forge: github` or `forge: forge`), else
   `forgejo.default_home`.
2. **New repo on the forge.** `repo create NAME` (private by default), then
   `git remote add origin {clone_url}` and `git -- push -u origin main`.
3. **Public as well?** If the user wants it on GitHub too and
   `forgejo.github_owner` is set, create the GitHub repo with the `github`
   skill, then `mirror push OWNER/REPO --to https://github.com/{github_owner}/NAME`.
   Every later push to the forge reaches GitHub by itself. Don't push to both.
4. **Branch and PR.** Commit, `git -- push origin BRANCH`, then `pr create`.
   Write the body to a file with `write_file` and pass `--body-file`.
5. **CI.** `runs OWNER/REPO` until the newest run for the branch is `success`
   or `failure`. Read failures on the run's `url`.
6. **Merge** only when the user asked: `pr merge`, which re-reads the PR and
   fails unless it is really merged.
7. **Events from the forge.** To have Hermes react to forge PRs or pushes, add a
   webhook route with `signature: github` (Forgejo signs like GitHub) and
   `deliver: forge_comment` so the answer lands on the PR. See the webhooks
   guide, "Forgejo / Gitea Webhook Setup".

## Pitfalls

- A repo made through the API is public to forge users unless created private;
  `repo create` is private unless you pass `--public`.
- A repo that mirrors both ways stops syncing at the first conflict. Commit on
  one side only: the forge, with GitHub as its push mirror.
- `mirror pull` needs an `https://` source; the forge refuses plain http.
- The forge stores `GITHUB_TOKEN` for each mirror it runs. Tell the user when
  you set one up.
- `git -- ...` adds the token only for the forge's address. Remotes on other
  hosts use the machine's normal git login.
- 404 right after a push is the forge catching up: `pr create` retries for a
  few seconds before giving up.

## Verification

- After `repo create`: `repo view OWNER/REPO` shows `"private": true` unless
  the user asked for public.
- After a push: `git -- ls-remote origin BRANCH` shows the commit you pushed.
- After `mirror push`: `mirror list` shows the GitHub address with no
  `last_error` once a push has gone through.
- Never report CI or merge state from memory: read `runs` or `pr view` again.
