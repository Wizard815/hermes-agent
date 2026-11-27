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
    forge.py job setup [--jobs-repo OWNER/hermes-jobs]       # once: the repo jobs run from
    forge.py job run --runner LABEL [--image IMG] [--repo OWNER/REPO] [--ref REF] [--wait] -- COMMAND
    forge.py job status RUN_ID [--logs N]
    forge.py runners                   # runner names and labels the token can see
    forge.py where REMOTE_URL          # "forge", "github" or "other"
    forge.py git -- push origin main   # any git command, with the token for the forge only
"""
from __future__ import annotations

import argparse
import base64
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


def api_text(url: str, path: str, timeout: float = 30) -> str:
    req = urllib.request.Request(f"{url}/api/v1{path}", headers={"Authorization": f"token {token()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ForgeError(f"forge {e.code} on GET {path}: {e.reason}")
    except urllib.error.URLError as e:
        raise ForgeError(f"can't reach the forge at {url}: {e.reason}")


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


# ---------------------------------------------------------------- jobs on the forge's runners
JOB_WORKFLOW = ".forgejo/workflows/hermes-job.yml"
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates", "hermes-job.yml")
DONE = {"success", "failure", "cancelled", "skipped"}


def jobs_repo(a) -> tuple[str, str]:
    if a.jobs_repo:
        return split_repo(a.jobs_repo)
    return api(a.url, "GET", "/user")["login"], "hermes-jobs"


def cmd_job_setup(a):
    owner, name = jobs_repo(a)
    if api(a.url, "GET", f"/repos/{owner}/{name}", ok404=True) is None:
        me = api(a.url, "GET", "/user")["login"]
        path = "/user/repos" if owner == me else f"/orgs/{quote(owner)}/repos"
        api(a.url, "POST", path, {"name": name, "private": True, "auto_init": True,
                                  "description": "Jobs Hermes runs on the forge's runners (forge.py job)"})
    with open(TEMPLATE, encoding="utf-8") as f:
        wanted = f.read()
    cur = api(a.url, "GET", f"/repos/{owner}/{name}/contents/{JOB_WORKFLOW}", ok404=True)
    content = base64.b64encode(wanted.encode()).decode()
    if cur is None:
        api(a.url, "POST", f"/repos/{owner}/{name}/contents/{JOB_WORKFLOW}", {"content": content, "message": "hermes job workflow"})
        workflow = "created"
    elif base64.b64decode(cur.get("content") or "").decode("utf-8", "replace") != wanted:
        api(a.url, "PUT", f"/repos/{owner}/{name}/contents/{JOB_WORKFLOW}",
            {"content": content, "sha": cur["sha"], "message": "update hermes job workflow"})
        workflow = "updated"
    else:
        workflow = "unchanged"
    # Jobs check out other forge repos with JOBS_TOKEN: a read-only token is best.
    read_token = os.environ.get("FORGE_JOBS_TOKEN", "").strip() or token()
    api(a.url, "PUT", f"/repos/{owner}/{name}/actions/secrets/JOBS_TOKEN", {"data": read_token})
    return {"jobs_repo": f"{owner}/{name}", "workflow": workflow,
            "checkout_token": "FORGE_JOBS_TOKEN" if os.environ.get("FORGE_JOBS_TOKEN", "").strip() else "FORGE_TOKEN"}


def _job_view(a, owner, name, run_id, logs: int = 0):
    run = api(a.url, "GET", f"/repos/{owner}/{name}/actions/runs/{int(run_id)}")
    jobs = api(a.url, "GET", f"/repos/{owner}/{name}/actions/runs/{int(run_id)}/jobs") or []
    ran = [j for j in jobs if j.get("status") != "skipped"] or jobs
    out = {"run": int(run_id), "status": run.get("status"), "title": run.get("title"), "url": run.get("html_url"),
           "jobs": [{"name": j.get("name"), "status": j.get("status"), "runs_on": j.get("runs_on")} for j in ran]}
    if logs and ran and ran[0].get("status") in DONE:
        text = api_text(a.url, f"/repos/{owner}/{name}/actions/jobs/{int(ran[0]['id'])}/logs")
        out["output"] = job_output(text, logs).replace(token(), "****")
    return out


_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")


def job_output(log: str, limit: int) -> str:
    """The command's own lines (after the workflow's marker), without timestamps; the log tail if no marker."""
    lines = [_STAMP.sub("", line) for line in log.rstrip().splitlines()]
    if "=== hermes job output ===" in lines:
        lines = lines[lines.index("=== hermes job output ===") + 1:]
        end = next((i for i, line in enumerate(lines) if line.startswith(("⭐ Run Post", "🏁"))), len(lines))
        lines = lines[:end]
    return "\n".join(lines[-limit:])


def cmd_job_run(a):
    owner, name = jobs_repo(a)
    command = " ".join(a.command).strip()
    if not command:
        raise ForgeError("usage: forge.py job run --runner LABEL -- COMMAND")
    if a.repo:
        split_repo(a.repo)
    inputs = {"runner": a.runner, "image": a.image or "", "repo": a.repo or "", "ref": a.ref or "",
              "command": command, "title": (a.title or command)[:120]}
    try:
        run = api(a.url, "POST", f"/repos/{owner}/{name}/actions/workflows/hermes-job.yml/dispatches",
                  {"ref": "main", "inputs": inputs, "return_run_info": True})
    except ForgeError as e:
        if " 404 " in str(e):
            raise ForgeError(f"no job workflow in {owner}/{name}: run `forge.py job setup` once") from None
        raise
    if not a.wait:
        return _job_view(a, owner, name, run["id"])
    deadline = time.time() + a.timeout
    while True:
        view = _job_view(a, owner, name, run["id"], logs=a.logs)
        if view["status"] in DONE:
            return view
        if time.time() > deadline:
            view["note"] = f"still {view['status']} after {a.timeout}s; check again with `job status {run['id']}`"
            if view["status"] == "waiting":
                view["note"] += f". Waiting means no idle runner has the label {a.runner!r}"
            return view
        time.sleep(5)


def cmd_job_status(a):
    owner, name = jobs_repo(a)
    return _job_view(a, owner, name, a.run_id, logs=a.logs)


def cmd_runners(a):
    for path in ("/admin/actions/runners", "/user/actions/runners"):
        try:
            data = api(a.url, "GET", path)
        except ForgeError:
            continue
        items = data if isinstance(data, list) else data.get("runners") or data.get("entries") or []
        return [{"name": r.get("name"), "labels": [lb.get("name") if isinstance(lb, dict) else lb for lb in r.get("labels") or []],
                 "status": r.get("status")} for r in items]
    raise ForgeError("the token can't list runners (needs an admin token); ask the user which labels exist, e.g. ubuntu-latest and gpu")


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

    j = sub.add_parser("job")
    j.add_argument("action", choices=["setup", "run", "status"])
    j.add_argument("run_id", nargs="?", help="run id for status")
    j.add_argument("--jobs-repo", help="OWNER/REPO jobs run from (default: YOU/hermes-jobs, or skills.config.forgejo.jobs_repo)")
    j.add_argument("--runner", default="ubuntu-latest", help="runner label: ubuntu-latest, gpu, ...")
    j.add_argument("--image", help="container image for the job; default: the runner label's own")
    j.add_argument("--repo", help="OWNER/REPO on the forge to check out first")
    j.add_argument("--ref", help="branch, tag or commit of --repo")
    j.add_argument("--title")
    j.add_argument("--wait", action="store_true", help="wait until it finishes and print the log tail")
    j.add_argument("--timeout", type=int, default=1800, help="seconds --wait waits")
    j.add_argument("--logs", type=int, default=60, help="log lines to show when done")

    sub.add_parser("runners")

    w = sub.add_parser("where")
    w.add_argument("remote")

    g = sub.add_parser("git")
    g.add_argument("--timeout", type=int, default=900, help="seconds before git is stopped")
    g.add_argument("args", nargs=argparse.REMAINDER)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command: list[str] = []
    if "job" in argv and "--" in argv:  # job run ... -- COMMAND: everything after -- is the command
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1:]
    a = parser().parse_args(argv)
    if a.cmd == "job":
        a.command = command
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
        if a.cmd == "job" and a.action == "status" and not a.run_id:
            raise ForgeError("job status needs a RUN_ID")
        if a.cmd == "job":
            result = {"setup": cmd_job_setup, "run": cmd_job_run, "status": cmd_job_status}[a.action](a)
        else:
            result = {"whoami": cmd_whoami, "repo": cmd_repo, "pr": cmd_pr, "issue": cmd_issue,
                      "runs": cmd_runs, "mirror": cmd_mirror, "where": cmd_where, "runners": cmd_runners}[a.cmd](a)
        if a.cmd == "job" and a.action != "setup" and result.get("status") in ("failure", "cancelled"):
            print(json.dumps(result, indent=2))
            return 1
    except ForgeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
