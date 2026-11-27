#!/usr/bin/env python3
"""Forgejo / Gitea from the terminal: repos, PRs, issues, CI runs, mirrors, git with the token.

Standard library only. The forge address comes from --url, else FORGE_URL;
the token from FORGE_TOKEN (never a flag, so it stays out of shell history
and process lists). Output is JSON on stdout; errors go to stderr with exit 1.

    forge.py whoami
    forge.py repo create NAME [--public] [--org ORG] [--description TEXT]
    forge.py repo view OWNER/REPO
    forge.py pr create OWNER/REPO --head BRANCH --base main --title T [--body-file F]
    forge.py pr list OWNER/REPO [--state open|closed|all]
    forge.py pr view OWNER/REPO NUMBER
    forge.py pr merge OWNER/REPO NUMBER [--style merge|squash|rebase]
    forge.py issue create OWNER/REPO --title T [--body-file F]
    forge.py issue list OWNER/REPO [--state open|closed|all]
    forge.py runs OWNER/REPO [--limit N]
    forge.py mirror push OWNER/REPO --to https://github.com/O/R   (uses GITHUB_TOKEN)
    forge.py mirror pull https://github.com/O/R [--name N] [--public]
    forge.py mirror list OWNER/REPO
    forge.py mirror sync OWNER/REPO
    forge.py where REMOTE_URL          # "forge", "github" or "other"
    forge.py git -- push origin main   # any git command, with the token for the forge only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit


class ForgeError(Exception):
    pass


def base_url(arg: str | None) -> str:
    url = (arg or os.environ.get("FORGE_URL") or "").strip().rstrip("/")
    if not url:
        raise ForgeError("no forge address: pass --url (skills.config.forgejo.url) or set FORGE_URL")
    if not url.startswith(("http://", "https://")):
        raise ForgeError(f"forge address must start with http:// or https://, got {url!r}")
    return url


def token() -> str:
    tok = os.environ.get("FORGE_TOKEN", "").strip()
    if not tok:
        raise ForgeError("FORGE_TOKEN is not set: make one on the forge (Settings > Applications) and add it to ~/.hermes/.env")
    return tok


def api(url: str, method: str, path: str, body: dict | None = None, ok404: bool = False, timeout: float = 30):
    req = urllib.request.Request(
        f"{url}/api/v1{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"token {token()}", "Accept": "application/json",
                 **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        try:
            msg = json.loads(e.read() or b"{}").get("message", "")
        except ValueError:
            msg = ""
        raise ForgeError(f"forge {e.code} on {method} {path}: {msg or e.reason}")
    except urllib.error.URLError as e:
        raise ForgeError(f"can't reach the forge at {url}: {e.reason}. Is it running, and is the address right?")


def split_repo(full: str) -> tuple[str, str]:
    m = re.fullmatch(r"([\w.-]+)/([\w.-]+?)(?:\.git)?", full or "")
    if not m:
        raise ForgeError(f"expected OWNER/REPO, got {full!r}")
    return m.group(1), m.group(2)


def read_body(path: str | None) -> str:
    if not path:
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- where a remote lives
_GITHUB = re.compile(r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)")


def where(remote: str, url: str | None) -> str:
    if _GITHUB.match(remote or ""):
        return "github"
    if not url:
        return "other"
    forge_host = (urlsplit(url).hostname or "").lower()
    extra = {h.strip().split(":")[0].lower() for h in os.environ.get("FORGE_HOSTS", "").split(",") if h.strip()}
    if "://" in remote:
        host = (urlsplit(remote).hostname or "").lower()
    else:
        m = re.match(r"^(?:[^@/]+@)?([^:/]+):", remote or "")
        host = m.group(1).lower() if m else ""
    return "forge" if host and host in ({forge_host} | extra) else "other"


def git_env(url: str) -> dict[str, str]:
    """git config through the environment: the token goes to the forge's address only."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = f"http.{url}/.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: token {token()}"
    return env


# ---------------------------------------------------------------- commands
def cmd_whoami(a):
    me = api(a.url, "GET", "/user")
    return {"login": me["login"], "url": a.url, "version": api(a.url, "GET", "/version").get("version")}


def cmd_repo(a):
    if a.action == "create":
        body = {"name": a.name, "private": not a.public, "description": a.description or ""}
        r = api(a.url, "POST", f"/orgs/{quote(a.org)}/repos" if a.org else "/user/repos", body)
        return {"full_name": r["full_name"], "private": r["private"], "clone_url": r["clone_url"],
                "ssh_url": r.get("ssh_url"), "html_url": r["html_url"]}
    owner, repo = split_repo(a.name)
    r = api(a.url, "GET", f"/repos/{owner}/{repo}")
    return {k: r.get(k) for k in ("full_name", "private", "mirror", "original_url", "default_branch",
                                  "clone_url", "html_url", "open_issues_count", "open_pr_counter")}


def _pr_summary(p: dict) -> dict:
    return {"number": p["number"], "title": p["title"], "state": "merged" if p.get("merged") else p["state"],
            "head": p["head"]["ref"], "base": p["base"]["ref"], "url": p["html_url"],
            "mergeable": p.get("mergeable")}


def cmd_pr(a):
    owner, repo = split_repo(a.repo)
    if a.action == "create":
        body = {"title": a.title, "head": a.head, "base": a.base, "body": read_body(a.body_file)}
        for attempt in range(6):  # the forge records a fresh push in the background
            try:
                return {**_pr_summary(api(a.url, "POST", f"/repos/{owner}/{repo}/pulls", body)), "existing": False}
            except ForgeError as e:
                if " 404 " in str(e) and attempt < 5:
                    time.sleep(1)
                    continue
                if " 409 " not in str(e):
                    raise
                existing = api(a.url, "GET", f"/repos/{owner}/{repo}/pulls/{quote(a.base)}/{quote(a.head)}", ok404=True)
                if not existing:
                    raise
                return {**_pr_summary(existing), "existing": True}
    if a.action == "list":
        return [_pr_summary(p) for p in api(a.url, "GET", f"/repos/{owner}/{repo}/pulls?state={a.state}&limit=50")]
    if a.action == "view":
        p = api(a.url, "GET", f"/repos/{owner}/{repo}/pulls/{int(a.number)}")
        return {**_pr_summary(p), "body": p.get("body") or ""}
    api(a.url, "POST", f"/repos/{owner}/{repo}/pulls/{int(a.number)}/merge", {"Do": a.style})
    p = api(a.url, "GET", f"/repos/{owner}/{repo}/pulls/{int(a.number)}")
    if not p.get("merged"):
        raise ForgeError(f"PR #{a.number} is not merged after the merge call; check it on the forge")
    return {**_pr_summary(p), "merged_at": p.get("merged_at")}


def cmd_issue(a):
    owner, repo = split_repo(a.repo)
    if a.action == "create":
        i = api(a.url, "POST", f"/repos/{owner}/{repo}/issues", {"title": a.title, "body": read_body(a.body_file)})
        return {"number": i["number"], "url": i["html_url"]}
    return [{"number": i["number"], "title": i["title"], "state": i["state"], "url": i["html_url"]}
            for i in api(a.url, "GET", f"/repos/{owner}/{repo}/issues?type=issues&state={a.state}&limit=50")]


def cmd_runs(a):
    owner, repo = split_repo(a.repo)
    data = api(a.url, "GET", f"/repos/{owner}/{repo}/actions/tasks?limit={int(a.limit)}") or {}
    return [{"id": r.get("id"), "workflow": r.get("workflow_id") or r.get("name"), "title": r.get("display_title"),
             "status": r.get("status"), "branch": r.get("head_branch"), "sha": (r.get("head_sha") or "")[:10],
             "url": r.get("url")} for r in data.get("workflow_runs") or []]


def cmd_mirror(a):
    if a.action == "pull":
        if not a.target.startswith("https://"):
            raise ForgeError("pull mirrors need an https:// source (the forge refuses plain http)")
        name = a.name or re.sub(r"\.git$", "", a.target.rstrip("/").rsplit("/", 1)[-1])
        body = {"clone_addr": a.target, "repo_name": name, "mirror": True, "private": not a.public,
                "mirror_interval": a.interval, "service": "git"}
        gh = os.environ.get("GITHUB_TOKEN", "").strip()
        if gh and _GITHUB.match(a.target):  # private GitHub sources; the forge keeps it to fetch later
            body.update(auth_username="x-access-token", auth_password=gh)
        r = api(a.url, "POST", "/repos/migrate", body, timeout=600)
        return {"full_name": r["full_name"], "mirror": r.get("mirror"), "private": r["private"], "html_url": r["html_url"]}
    owner, repo = split_repo(a.target)
    if a.action == "list":
        info = api(a.url, "GET", f"/repos/{owner}/{repo}")
        pushes = api(a.url, "GET", f"/repos/{owner}/{repo}/push_mirrors") or []
        return {"pull_from": info.get("original_url") if info.get("mirror") else None,
                "push_to": [{"name": m.get("remote_name"), "address": m.get("remote_address"),
                             "on_push": m.get("sync_on_commit"), "last_error": m.get("last_error") or None} for m in pushes]}
    if a.action == "sync":
        info = api(a.url, "GET", f"/repos/{owner}/{repo}")
        api(a.url, "POST", f"/repos/{owner}/{repo}/{'mirror-sync' if info.get('mirror') else 'push_mirrors-sync'}")
        return {"ok": True, "synced": "pull" if info.get("mirror") else "push"}
    # push
    if not a.to or not _GITHUB.match(a.to) or not a.to.startswith("https://"):
        raise ForgeError("--to must be an https://github.com/OWNER/REPO URL")
    gh = os.environ.get("GITHUB_TOKEN", "").strip()
    if not gh:
        raise ForgeError("a push mirror to GitHub needs GITHUB_TOKEN (write access to that repo)")
    address = re.sub(r"(\.git)?/?$", ".git", a.to)
    for m in api(a.url, "GET", f"/repos/{owner}/{repo}/push_mirrors") or []:
        if m.get("remote_address") == address:
            raise ForgeError(f"{owner}/{repo} already mirrors to {address}")
    api(a.url, "POST", f"/repos/{owner}/{repo}/push_mirrors", {
        "remote_address": address, "remote_username": "x-access-token", "remote_password": gh,
        "interval": a.interval, "sync_on_commit": True})
    return {"ok": True, "from": f"{owner}/{repo}", "to": address}


def cmd_where(a):
    try:
        url = base_url(a.url)
    except ForgeError:
        url = None
    return {"remote": a.remote, "forge": where(a.remote, url)}


def cmd_git(a):
    args = a.args[1:] if a.args[:1] == ["--"] else a.args
    if not args:
        raise ForgeError("usage: forge.py git -- <git arguments>")
    try:
        res = subprocess.run(["git", *args], env=git_env(a.url), capture_output=True, text=True, timeout=a.timeout, check=False)
    except subprocess.TimeoutExpired:
        raise ForgeError(f"git {args[0]} took longer than {a.timeout}s; pass --timeout for big repos") from None
    out = (res.stdout + res.stderr).replace(token(), "****")
    sys.stdout.write(out)
    return res.returncode


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--url", help="forge address, e.g. http://192.168.20.5:3030 (default: $FORGE_URL)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami")

    r = sub.add_parser("repo")
    r.add_argument("action", choices=["create", "view"])
    r.add_argument("name", help="NAME for create, OWNER/REPO for view")
    r.add_argument("--public", action="store_true", help="visible to everyone signed in to the forge (default private)")
    r.add_argument("--org")
    r.add_argument("--description")

    pr = sub.add_parser("pr")
    pr.add_argument("action", choices=["create", "list", "view", "merge"])
    pr.add_argument("repo")
    pr.add_argument("number", nargs="?")
    pr.add_argument("--head")
    pr.add_argument("--base", default="main")
    pr.add_argument("--title")
    pr.add_argument("--body-file")
    pr.add_argument("--state", default="open", choices=["open", "closed", "all"])
    pr.add_argument("--style", default="merge", choices=["merge", "squash", "rebase"])

    i = sub.add_parser("issue")
    i.add_argument("action", choices=["create", "list"])
    i.add_argument("repo")
    i.add_argument("--title")
    i.add_argument("--body-file")
    i.add_argument("--state", default="open", choices=["open", "closed", "all"])

    ru = sub.add_parser("runs")
    ru.add_argument("repo")
    ru.add_argument("--limit", default=10, type=int)

    m = sub.add_parser("mirror")
    m.add_argument("action", choices=["push", "pull", "list", "sync"])
    m.add_argument("target", help="OWNER/REPO on the forge, or the https source URL for pull")
    m.add_argument("--to")
    m.add_argument("--name")
    m.add_argument("--public", action="store_true")
    m.add_argument("--interval", default="8h")

    w = sub.add_parser("where")
    w.add_argument("remote")

    g = sub.add_parser("git")
    g.add_argument("--timeout", type=int, default=900, help="seconds before git is stopped")
    g.add_argument("args", nargs=argparse.REMAINDER)
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        if a.cmd != "where":
            a.url = base_url(a.url)
        if a.cmd == "git":
            return cmd_git(a)
        if a.cmd == "pr" and a.action == "create" and not (a.head and a.title):
            raise ForgeError("pr create needs --head and --title")
        if a.cmd == "pr" and a.action in ("view", "merge") and not a.number:
            raise ForgeError(f"pr {a.action} needs a NUMBER")
        if a.cmd == "issue" and a.action == "create" and not a.title:
            raise ForgeError("issue create needs --title")
        result = {"whoami": cmd_whoami, "repo": cmd_repo, "pr": cmd_pr, "issue": cmd_issue,
                  "runs": cmd_runs, "mirror": cmd_mirror, "where": cmd_where}[a.cmd](a)
    except ForgeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
