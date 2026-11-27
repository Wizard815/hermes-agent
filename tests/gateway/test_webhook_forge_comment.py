"""Forgejo/Gitea webhooks: their deliveries validate as GitHub's, and ``forge_comment`` replies on the forge.

Header set copied from a real Forgejo 17.0.1 delivery. The reply goes to a fake forge API on localhost.
"""

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_delivery import validate_destination
from gateway.webhook_targets import is_dispatchable_target

TOKEN = "forgetoken0000000000000000000000000000cd"


def _forgejo_headers(body: bytes, secret: str) -> dict:
    hexsig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Forgejo-Delivery": "6f64a685", "X-Forgejo-Event": "pull_request", "X-Forgejo-Signature": hexsig,
            "X-GitHub-Delivery": "6f64a685", "X-GitHub-Event": "pull_request", "X-Gitea-Signature": hexsig,
            "X-Hub-Signature": "sha1=ignored", "X-Hub-Signature-256": "sha256=" + hexsig}


def _request(headers):
    req = MagicMock()
    req.headers = headers
    req.match_info = {"route_name": "forge"}
    return req


def test_forgejo_delivery_validates_with_github_pin():
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))
    body = b'{"action":"opened","number":7}'
    assert adapter._validate_signature(_request(_forgejo_headers(body, "s3cret")), body, "s3cret", "github")
    assert not adapter._validate_signature(_request(_forgejo_headers(body, "other")), body, "s3cret", "github")


def test_forge_comment_is_a_known_target_and_keeps_only_repo_and_number():
    assert is_dispatchable_target("forge_comment")
    kept = validate_destination({"deliver": "forge_comment",
                                 "deliver_extra": {"repo": "wizard/r", "pr_number": "7", "url": "http://evil"}})
    assert kept == {"deliver": "forge_comment", "deliver_extra": {"repo": "wizard/r", "pr_number": "7"}}


class _FakeForge(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        _FakeForge.calls.append((self.path, self.headers.get("Authorization"), body))
        code = 201 if self.headers.get("Authorization") == f"token {TOKEN}" else 401
        if self.path.startswith("/api/v1/repos/wizard/missing/"):
            code = 404
        raw = b'{"id": 1}'
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def forge(monkeypatch):
    _FakeForge.calls = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeForge)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("FORGE_URL", f"http://127.0.0.1:{srv.server_port}/")
    monkeypatch.setenv("FORGE_TOKEN", TOKEN)
    yield srv
    srv.shutdown()


def _adapter():
    return WebhookAdapter.__new__(WebhookAdapter)


@pytest.mark.asyncio
async def test_forge_comment_posts_to_issue_comments_with_token_header(forge):
    res = await _adapter()._deliver_forge_comment(
        "Looks good", {"deliver": "forge_comment", "deliver_extra": {"repo": "wizard/r", "pr_number": "7"}})
    assert res.success
    path, auth, body = _FakeForge.calls[-1]
    assert path == "/api/v1/repos/wizard/r/issues/7/comments"
    assert auth == f"token {TOKEN}" and body == {"body": "Looks good"}


@pytest.mark.asyncio
async def test_forge_comment_says_what_went_wrong(forge, monkeypatch):
    a = _adapter()
    res = await a._deliver_forge_comment("x", {"deliver_extra": {"repo": "wizard/missing", "pr_number": "7"}})
    assert not res.success and "no such repo" in res.error
    for extra in ({"repo": "wizard/r"}, {"repo": "../x", "pr_number": "1"}, {"repo": "w/r", "pr_number": "-1"}):
        assert not (await a._deliver_forge_comment("x", {"deliver_extra": extra})).success
    calls = len(_FakeForge.calls)
    monkeypatch.setenv("FORGE_TOKEN", "wrong")
    res = await a._deliver_forge_comment("x", {"deliver_extra": {"repo": "wizard/r", "pr_number": "7"}})
    assert not res.success and "refused FORGE_TOKEN" in res.error and len(_FakeForge.calls) == calls + 1
    monkeypatch.delenv("FORGE_TOKEN")
    res = await a._deliver_forge_comment("x", {"deliver_extra": {"repo": "wizard/r", "pr_number": "7"}})
    assert not res.success and "FORGE_TOKEN is not set" in res.error


@pytest.mark.asyncio
async def test_forge_comment_needs_a_configured_address(monkeypatch):
    monkeypatch.delenv("FORGE_URL", raising=False)
    monkeypatch.setenv("FORGE_TOKEN", TOKEN)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", dict)
    res = await _adapter()._deliver_forge_comment("x", {"deliver_extra": {"repo": "wizard/r", "pr_number": "7"}})
    assert not res.success and "No forge address" in res.error
