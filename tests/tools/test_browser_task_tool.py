"""browser_task (tools/browser_task_tool.py): browser-use's own agent loop on Hermes's browser.

A fake runner speaks the JSON-lines protocol of tools/browser_task/runner.py; the real runner and a real
model are exercised by the live check at the end (HERMES_LIVE_BROWSER_TASK=1).
"""
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.request

import pytest

import tools.browser_task_tool as bt

_FAKE_RUNNER = textwrap.dedent("""\
    import json, os, sys
    if "--check" in sys.argv:
        print(json.dumps({"ok": True, "version": "fake"})); sys.exit(0)
    req = json.loads(sys.stdin.read())
    out = lambda e: print(json.dumps(e), flush=True)
    out({"type": "started", "version": "fake"})
    out({"type": "step", "n": 1, "text": "goto_url(" + json.dumps(req["cdp_url"]) + ")"})
    out({"type": "step", "n": 2, "text": "key=" + os.environ.get("BROWSER_TASK_API_KEY", "")
         + " pythonpath=" + os.environ.get("PYTHONPATH", "-") + " argv=" + " ".join(sys.argv[1:])})
    if req["task"] == "crash":
        sys.stderr.write("Traceback: boom\\n"); sys.exit(3)
    out({"type": "done", "status": "completed", "success": True, "text": req["model"] + "@" + req["base_url"]
         + " steps=" + str(req["max_steps"]), "steps": 2, "cost_usd": 0.001, "error": None})
""")


@pytest.fixture
def fake_runner(tmp_path, monkeypatch):
    runner = tmp_path / "runner.py"
    runner.write_text(_FAKE_RUNNER)
    monkeypatch.setattr(bt, "_RUNNER", runner)
    monkeypatch.setattr(bt, "_task_cfg", lambda: {})
    bt._python_cache.clear()
    return runner


_MODEL = {"model": "gemma-4-31b", "base_url": "http://argos.lan:8080/v1", "api_key": "sk-secret"}
_CDP = {"BU_CDP_URL": "http://127.0.0.1:9222", "PYTHONPATH": "/hermes/site", "PATH": os.environ.get("PATH", "")}


def test_BrowserTask_runsAndReportsSteps(fake_runner):
    result = bt.run_browser_task("Find the heading", python=sys.executable, cdp_env=dict(_CDP), model=dict(_MODEL))
    assert result["status"] == "completed" and result["success"] is True
    assert result["result"] == "gemma-4-31b@http://argos.lan:8080/v1 steps=25"
    assert result["steps"][0] == 'goto_url("http://127.0.0.1:9222")'
    # The key goes by environment only, and Hermes's PYTHONPATH never reaches browser-use's venv.
    assert result["steps"][1] == "key=sk-secret pythonpath=- argv="
    assert result["cost_usd"] == 0.001


def test_BrowserTask_reportsACrashWithItsReason(fake_runner):
    result = bt.run_browser_task("crash", python=sys.executable, cdp_env=dict(_CDP), model=dict(_MODEL))
    assert result["status"] == "error"
    assert "exit 3" in result["error"] and "boom" in result["error"]


def test_BrowserTask_checksTheRequest(fake_runner):
    assert "Say what the browser should do" in bt.run_browser_task("  ", python=sys.executable)["error"]


def test_BrowserTask_saysHowToInstallWhenBrowserUseIsMissing(monkeypatch):
    monkeypatch.setattr(bt, "browser_use_python", lambda refresh=False: None)
    result = bt.run_browser_task("Find the heading")
    assert result["status"] == "error" and "browser-use==1.0.4" in result["error"]


def test_BrowserTask_findsAPythonWithBrowserUse(fake_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(bt, "_home_venv_python", lambda: str(tmp_path / "missing" / "python"))
    assert bt.browser_use_python(refresh=True) in (None, "/opt/browser-use/bin/python")
    monkeypatch.setattr(bt, "_task_cfg", lambda: {"python": sys.executable})
    assert bt.browser_use_python(refresh=True) == sys.executable
    assert bt.browser_task_available() is True


def test_BrowserTask_modelFromConfigOrTheMainModel(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-argos")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    model, err = bt.resolve_task_model({"model": "gemma-4-31b", "base_url": "http://argos:8080/v1/"})
    assert err is None and model == {"model": "gemma-4-31b", "base_url": "http://argos:8080/v1", "api_key": "sk-argos"}
    model, err = bt.resolve_task_model({"model": "anthropic/claude-haiku-5.5", "base_url": "https://openrouter.ai/api/v1"})
    assert model["api_key"] == "sk-or"
    monkeypatch.setenv("ARGOS_KEY", "sk-own")
    model, _ = bt.resolve_task_model({"model": "m", "base_url": "http://x/v1", "api_key_env": "ARGOS_KEY"})
    assert model["api_key"] == "sk-own"
    import hermes_cli.config_effective as ce
    monkeypatch.setattr(ce, "load_user_config_effective", lambda: {"model": {"provider": "custom", "base_url": "http://192.168.20.5:8080/v1", "default": "qwen3.6-35b-a3b"}})
    model, err = bt.resolve_task_model({})
    assert err is None and model["model"] == "qwen3.6-35b-a3b" and model["base_url"] == "http://192.168.20.5:8080/v1"
    monkeypatch.setattr(ce, "load_user_config_effective", lambda: {"model": {"provider": "openrouter", "default": "anthropic/claude-haiku-5.5"}})
    model, _ = bt.resolve_task_model({})
    assert model["base_url"] == "https://openrouter.ai/api/v1" and model["api_key"] == "sk-or"
    monkeypatch.setattr(ce, "load_user_config_effective", lambda: {})
    model, err = bt.resolve_task_model({})
    assert model is None and "browser.task.model" in err


def test_BrowserTask_isGatedLikeBrowserExec():
    import model_tools
    rewriters = model_tools._DYNAMIC_SCHEMA_REWRITERS
    td = {"type": "function", "function": {"name": "browser_task", "description": "x"}}
    assert rewriters["browser_task"](td, {"browser_task"}) is None
    assert rewriters["browser_task"](td, {"browser_task", "terminal"}) == td


@pytest.mark.skipif(os.environ.get("HERMES_LIVE_BROWSER_TASK") != "1", reason="set HERMES_LIVE_BROWSER_TASK=1")
def test_BrowserTask_live(tmp_path):
    """Real browser-use (LIVE_BROWSER_USE_PYTHON), a real Chromium over CDP, a real model
    (LIVE_BROWSER_TASK_BASE_URL / LIVE_BROWSER_TASK_MODEL / LIVE_BROWSER_TASK_KEY)."""
    chrome = os.environ.get("LIVE_CHROME", "/opt/pw-browsers/chromium/chrome-linux64/chrome")
    proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--remote-debugging-port=0",
                             f"--user-data-dir={tmp_path / 'profile'}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = tmp_path / "profile" / "DevToolsActivePort"
        deadline = time.time() + 20
        while not port_file.exists() and time.time() < deadline:
            time.sleep(0.1)
        cdp = f"http://127.0.0.1:{port_file.read_text().split()[0]}"
        result = bt.run_browser_task(
            'Open https://example.com, click its "Learn more" link, and answer with the title of the page you land on.',
            max_steps=12, timeout_s=300, python=os.environ["LIVE_BROWSER_USE_PYTHON"],
            cdp_env={"BU_CDP_URL": cdp, "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
                     **{k: v for k, v in os.environ.items() if k.upper() in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE")}},
            model={"model": os.environ.get("LIVE_BROWSER_TASK_MODEL", "anthropic/claude-haiku-5.5"),
                   "base_url": os.environ.get("LIVE_BROWSER_TASK_BASE_URL", "https://openrouter.ai/api/v1"),
                   "api_key": os.environ.get("LIVE_BROWSER_TASK_KEY", "")})
        print(json.dumps(result, indent=1))
        assert result["status"] == "completed", result
        assert "Example Domains" in (result["result"] or "")
        # The page the task left is still open for browser_exec.
        tabs = json.load(urllib.request.urlopen(f"{cdp}/json/list"))
        assert any("iana.org" in t.get("url", "") for t in tabs), tabs
    finally:
        proc.terminate()
        proc.wait(timeout=10)
