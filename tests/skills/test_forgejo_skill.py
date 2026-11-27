"""Behaviour of the forgejo skill's helper against a fake Forgejo API on localhost."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "skills/software-development/forgejo/scripts/forge.py"

spec = importlib.util.spec_from_file_location("forgejo_helper", HELPER)
forge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forge)

TOKEN = "forgetoken0000000000000000000000000000ab"


class FakeForge(BaseHTTPRequestHandler):
    calls: list = []
    pr_posts = 0
    jobs_repo = False

    def log_message(self, *a):
        pass

    def _send(self, code, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        FakeForge.calls.append((method, self.path, body, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != f"token {TOKEN}":
            return self._send(401, {"message": "token is required"})
        p = self.path
        pr = {"number": 7, "title": "T", "state": "open", "merged": False, "head": {"ref": "feat"},
              "base": {"ref": "main"}, "html_url": "http://forge/w/r/pulls/7", "mergeable": True}
        if p == "/api/v1/user":
            return self._send(200, {"login": "wizard"})
        if p == "/api/v1/version":
            return self._send(200, {"version": "17.0.1"})
        if p == "/api/v1/user/repos" and method == "POST":
            return self._send(201, {"full_name": f"wizard/{body['name']}", "private": body["private"],
                                    "clone_url": "http://forge/x.git", "html_url": "http://forge/x"})
        if p == "/api/v1/repos/w/r/pulls" and method == "POST":
            FakeForge.pr_posts += 1
            if FakeForge.pr_posts == 1:
                return self._send(404, {"message": "branch not found yet"})
            if FakeForge.pr_posts == 2:
                return self._send(201, pr)
            return self._send(409, {"message": "pull request already exists"})
        if p == "/api/v1/repos/w/r" and method == "GET":
            return self._send(200, {"full_name": "w/r", "default_branch": "trunk"})
        if p == "/api/v1/repos/w/r/pulls/main/feat":
            return self._send(200, pr)
        if p == "/api/v1/repos/w/r/push_mirrors":
            return self._send(200 if method == "GET" else 201, [] if method == "GET" else {})
        if p == "/api/v1/repos/w/r/pulls/7/merge":
            return self._send(200)
        if p == "/api/v1/repos/w/r/pulls/7":
            return self._send(200, {**pr, "merged": False})
        if p == "/api/v1/repos/wizard/hermes-jobs" and method == "GET":
            return self._send(200 if FakeForge.jobs_repo else 404, {"full_name": "wizard/hermes-jobs"})
        if p == "/api/v1/user/repos" and method == "POST":
            FakeForge.jobs_repo = True
            return self._send(201, {"full_name": "wizard/hermes-jobs", "private": True})
        if p == "/api/v1/repos/wizard/hermes-jobs/contents/.forgejo/workflows/hermes-job.yml":
            return self._send(404 if method == "GET" else 201, {"message": "none"} if method == "GET" else {})
        if p == "/api/v1/repos/wizard/hermes-jobs/actions/secrets/JOBS_TOKEN":
            return self._send(404 if method == "DELETE" else 201, {"message": "none"} if method == "DELETE" else None)
        if p == "/api/v1/repos/wizard/hermes-jobs/actions/workflows/hermes-job.yml/dispatches":
            return self._send(201, {"id": 41, "run_number": 3, "jobs": ["run", "run-in-image"]})
        if p == "/api/v1/repos/wizard/hermes-jobs/actions/runs/41":
            return self._send(200, {"status": "failure", "title": "t", "html_url": "http://forge/runs/3"})
        if p == "/api/v1/repos/wizard/hermes-jobs/actions/runs/41/jobs":
            return self._send(200, [{"id": 5, "name": "run", "status": "failure", "runs_on": ["gpu"]},
                                    {"id": 6, "name": "run-in-image", "status": "skipped", "runs_on": ["gpu"]}])
        if p == "/api/v1/repos/wizard/hermes-jobs/actions/jobs/5/logs":
            raw = ("2026-11-27T15:49:52.5Z ⭐ Run Main actions/checkout@v4\n2026-11-27T15:49:53.2Z === hermes job output ===\n"
                   f"2026-11-27T15:49:53.3Z rocm ok {TOKEN}\n2026-11-27T15:49:53.4Z ⭐ Run Post actions/checkout@v4\n").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        return self._send(404, {"message": "not here"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


@pytest.fixture
def server(monkeypatch):
    FakeForge.calls, FakeForge.pr_posts, FakeForge.jobs_repo = [], 0, False
    srv = HTTPServer(("127.0.0.1", 0), FakeForge)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("FORGE_TOKEN", TOKEN)
    monkeypatch.delenv("FORGE_URL", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(forge.time, "sleep", lambda s: None)
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def run(url, *args, capsys=None):
    code = forge.main(["--url", url, *args])
    out = capsys.readouterr() if capsys else None
    return code, out


def test_repo_create_is_private_by_default(server, capsys):
    code, out = run(server, "repo", "create", "notes", capsys=capsys)
    assert code == 0 and json.loads(out.out)["private"] is True
    assert FakeForge.calls[-1][2]["private"] is True


def test_pr_create_retries_after_push_and_finds_existing(server, capsys):
    code, out = run(server, "pr", "create", "w/r", "--head", "feat", "--title", "T", capsys=capsys)
    assert code == 0 and json.loads(out.out) == {**json.loads(out.out), "number": 7, "existing": False}
    code, out = run(server, "pr", "create", "w/r", "--head", "feat", "--title", "T", capsys=capsys)
    assert code == 0 and json.loads(out.out)["existing"] is True


def test_merge_fails_unless_really_merged(server, capsys):
    code, out = run(server, "pr", "merge", "w/r", "7", capsys=capsys)
    assert code == 1 and "not merged" in out.err


def test_token_only_in_header_never_in_output(server, capsys):
    run(server, "whoami", capsys=capsys)
    assert all(c[3] == f"token {TOKEN}" for c in FakeForge.calls)


def test_missing_token_or_address_says_what_to_do(server, monkeypatch, capsys):
    monkeypatch.delenv("FORGE_TOKEN")
    code, out = run(server, "whoami", capsys=capsys)
    assert code == 1 and "FORGE_TOKEN is not set" in out.err
    assert forge.main(["whoami"]) == 1
    assert "no forge address" in capsys.readouterr().err


def test_push_mirror_needs_github_url_and_token(server, monkeypatch, capsys):
    code, out = run(server, "mirror", "push", "w/r", "--to", "https://gitlab.com/w/r", capsys=capsys)
    assert code == 1 and "github.com" in out.err
    code, out = run(server, "mirror", "push", "w/r", "--to", "https://github.com/W/r", capsys=capsys)
    assert code == 1 and "GITHUB_TOKEN" in out.err
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    code, out = run(server, "mirror", "push", "w/r", "--to", "https://github.com/W/r", capsys=capsys)
    assert code == 0
    sent = FakeForge.calls[-1][2]
    assert sent["remote_address"] == "https://github.com/W/r.git" and sent["sync_on_commit"] is True


def test_pull_mirror_refuses_plain_http(server, capsys):
    code, out = run(server, "mirror", "pull", "http://lan/w/r.git", capsys=capsys)
    assert code == 1 and "https" in out.err


@pytest.mark.parametrize("remote,expected", [
    ("http://192.168.20.5:3030/w/r.git", "forge"),
    ("ssh://git@192.168.20.5:2224/w/r.git", "forge"),
    ("git@forgejo:w/r.git", "forge"),
    ("https://github.com/w/r.git", "github"),
    ("git@github.com:w/r.git", "github"),
    ("https://gitlab.com/w/r.git", "other"),
])
def test_where_tells_forge_from_github(monkeypatch, remote, expected):
    monkeypatch.setenv("FORGE_HOSTS", "forgejo:3000")
    assert forge.where(remote, "http://192.168.20.5:3030") == expected


def test_git_env_scopes_token_to_forge_address(monkeypatch):
    monkeypatch.setenv("FORGE_TOKEN", TOKEN)
    env = forge.git_env("http://192.168.20.5:3030")
    assert env["GIT_CONFIG_KEY_0"] == "http.http://192.168.20.5:3030/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: token {TOKEN}"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_job_setup_makes_private_repo_workflow_and_secret(server, monkeypatch, capsys):
    monkeypatch.delenv("FORGE_JOBS_TOKEN", raising=False)
    code, out = run(server, "job", "setup", capsys=capsys)
    res = json.loads(out.out)
    assert code == 0 and res["workflow"] == "created" and res["checkout_token"].startswith("the run's own token")
    posts = {c[1]: c[2] for c in FakeForge.calls if c[0] in ("POST", "PUT")}
    assert posts["/api/v1/user/repos"]["private"] is True
    assert b"workflow_dispatch" in forge.base64.b64decode(posts["/api/v1/repos/wizard/hermes-jobs/contents/.forgejo/workflows/hermes-job.yml"]["content"])
    # FORGE_TOKEN can write every repo: it never becomes the jobs' secret, and an old copy is deleted
    secret = "/api/v1/repos/wizard/hermes-jobs/actions/secrets/JOBS_TOKEN"
    assert secret not in posts and ("DELETE", secret) in [(c[0], c[1]) for c in FakeForge.calls]
    monkeypatch.setenv("FORGE_JOBS_TOKEN", "readonly")
    run(server, "job", "setup", capsys=capsys)
    assert FakeForge.calls[-1][:3] == ("PUT", secret, {"data": "readonly"})


def test_job_checkout_leaves_no_token_for_the_command():
    wf = forge.TEMPLATE.read_text() if hasattr(forge.TEMPLATE, "read_text") else open(forge.TEMPLATE).read()
    assert wf.count("uses: actions/checkout@v4") == wf.count("persist-credentials: false") == 2
    assert wf.count("${{ secrets.JOBS_TOKEN || github.token }}") == 2


def test_job_run_dispatches_waits_and_shows_only_command_output(server, capsys):
    code, out = run(server, "job", "run", "--runner", "gpu", "--repo", "w/r", "--wait", "--", "rocminfo", "|", "head", capsys=capsys)
    assert code == 1  # the job failed
    res = json.loads(out.out)
    sent = next(c[2] for c in FakeForge.calls if c[1].endswith("/dispatches"))
    assert sent["inputs"]["runner"] == "gpu" and sent["inputs"]["repo"] == "w/r" and sent["inputs"]["ref"] == "trunk" and sent["inputs"]["command"] == "rocminfo | head"
    assert res["status"] == "failure" and res["jobs"] == [{"name": "run", "status": "failure", "runs_on": ["gpu"]}]
    assert res["output"] == "rocm ok ****"   # no checkout noise, token masked


def test_job_run_needs_a_command(server, capsys):
    code, out = run(server, "job", "run", "--runner", "gpu", capsys=capsys)
    assert code == 1 and "COMMAND" in out.err


def test_job_output_stops_at_runner_lines_and_keeps_the_exit_code():
    log = ("2026-11-27T10:00:00.1Z setup\n2026-11-27T10:00:01.2Z === hermes job output ===\n"
           "2026-11-27T10:00:02.3Z out\n2026-11-27T10:00:02.4Z ⚙️ [runner]: exitcode '5': failure\n"
           "2026-11-27T10:00:02.5Z skipping post step for 'actions/checkout@v4'; main step was skipped\n")
    assert forge.job_output(log, 50) == "out"
    assert forge._EXIT.search(log).group(1) == "5"
    # no marker (the image could not be pulled): the log's tail, which says why
    assert forge.job_output("a\nError response from daemon: pull failed\n", 1) == "Error response from daemon: pull failed"


def test_job_run_refuses_a_label_no_runner_has(server, monkeypatch, capsys):
    monkeypatch.setattr(forge, "runner_labels", lambda a: {"ubuntu-latest", "gpu"})
    code, out = run(server, "job", "run", "--runner", "gpu2", "--", "echo", "hi", capsys=capsys)
    assert code == 1 and "no runner has the label 'gpu2'" in out.err
