"""``browser_task``: hand a whole browser job to browser-use (https://github.com/browser-use/browser-use).

browser_exec makes the main model write every browser step. browser_task gives the job to browser-use 1.0's own
agent loop instead: one call, a goal in plain words, and a model of its own (``browser.task`` in config.yaml),
so a cheap or local model (Argos/llama.cpp) can do the clicking while the main model plans. It drives the SAME
browser browser_exec resolves (operator ``BU_CDP_*``, ``/browser connect``, a CDP cloud provider, or the local
Chromium), so the two tools share tabs and logins, and the page a task ends on stays open for browser_exec.

browser-use pins its own dependencies, so it lives in a venv of its own and runs as a subprocess
(tools/browser_task/runner.py); the model's key goes by environment, never argv. Docs:
website/docs/user-guide/features/browser.md#browser-tasks-browser-use.
"""

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

_RUNNER = Path(__file__).resolve().parent / "browser_task" / "runner.py"
_DEFAULT_MAX_STEPS = 25
_DEFAULT_TIMEOUT_S = 600
_MAX_TIMEOUT_S = 1800
_PYTHON_CANDIDATES = ("/opt/browser-use/bin/python",)
_OPENROUTER_URL = "https://openrouter.ai/api/v1"

_python_cache: dict[str, Any] = {}
_python_lock = threading.Lock()


def _task_cfg() -> dict:
    """``browser.task`` from config.yaml: model, base_url, api_key_env, python, max_steps, vision."""
    try:
        from tools.browser_session_cloud import browser_section
        section = browser_section().get("task")
        return section if isinstance(section, dict) else {}
    except Exception as e:  # noqa: BLE001 - a bad config disables the tool, it never raises
        logger.debug("Could not read browser.task: %s", e)
        return {}


def _home_venv_python() -> str:
    from hermes_constants import get_hermes_home
    return str(Path(get_hermes_home()) / "browser-use" / "bin" / "python")


def browser_use_python(refresh: bool = False) -> Optional[str]:
    """The first Python that imports browser_use: ``browser.task.python``, ``$HERMES_HOME/browser-use`` (the venv
    the docs set up), then /opt/browser-use. Cached per process, keyed by the candidates."""
    cfg_python = str(_task_cfg().get("python") or "").strip()
    candidates = [cfg_python, _home_venv_python(), *_PYTHON_CANDIDATES]
    key = "|".join(c or "" for c in candidates)
    with _python_lock:
        if not refresh and key in _python_cache:
            return _python_cache[key]
        found = None
        for python in candidates:
            if not python or not os.path.isfile(python):
                continue
            try:
                check = subprocess.run([python, "-I", str(_RUNNER), "--check"], capture_output=True, text=True,
                                       timeout=30)
                answer = json.loads((check.stdout or "").strip().splitlines()[-1] or "{}")
            except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
                continue
            if answer.get("ok"):
                found = python
                break
        _python_cache[key] = found
        return found


def browser_task_available() -> bool:
    """check_fn: a browser-use Python exists. Never starts a browser."""
    return browser_use_python() is not None


def _secret(name: str) -> str:
    from agent.secret_scope import get_secret
    return str(get_secret(name, "") or "")


def resolve_task_model(cfg: Optional[dict] = None) -> tuple[Optional[dict], Optional[str]]:
    """The model browser-use calls, as ``{"model", "base_url", "api_key"}``, or an error.

    ``browser.task.model`` / ``base_url`` / ``api_key_env`` win. Unset, the main model's OpenAI-compatible
    endpoint is used (``model.base_url``, e.g. Argos), or OpenRouter when ``model.provider`` is openrouter."""
    cfg = _task_cfg() if cfg is None else cfg
    model = str(cfg.get("model") or "").strip()
    base_url = str(cfg.get("base_url") or "").strip()
    key_env = str(cfg.get("api_key_env") or "").strip()
    if not (model and base_url):
        try:
            from hermes_cli.config_effective import load_user_config_effective
            main = load_user_config_effective().get("model")
        except Exception:  # noqa: BLE001
            main = None
        main = main if isinstance(main, dict) else {}
        provider = str(main.get("provider") or "").strip().lower()
        model = model or str(main.get("default") or main.get("model") or "").strip()
        if not base_url:
            base_url = str(main.get("base_url") or "").strip() or (_OPENROUTER_URL if provider == "openrouter" else "")
    if not model or not base_url:
        return None, ("browser_task has no model: set browser.task.model and browser.task.base_url in config.yaml "
                      "(any OpenAI-compatible endpoint: Argos, llama.cpp, OpenRouter).")
    if not key_env:
        key_env = "OPENROUTER_API_KEY" if "openrouter.ai" in base_url else "OPENAI_API_KEY"
    return {"model": model, "base_url": base_url.rstrip("/"), "api_key": _secret(key_env)}, None


def _resolve_cdp(task_id: Optional[str]) -> tuple[Optional[dict], Optional[str], bool]:
    """The env browser_exec would use for this task: (env with BU_CDP_*, error, on the Bot Desktop browser)."""
    from tools import browser_use_cli as bu
    env = bu._base_subprocess_env()
    route_key = bu._backend_cache_key(task_id, "")
    env, route_err, _sidecar, _generation = bu._route_exec_current(env, "", task_id, False, False, route_key)
    if route_err:
        return None, route_err, False
    bot_desktop = bool(env.pop(bu._BOT_DESKTOP_BROWSER_SENTINEL, None))
    for sentinel in (bu._PRIVATE_BROWSER_SENTINEL, bu._CLOUD_BROWSER_SENTINEL, bu._LOCAL_FALLBACK_SENTINEL,
                     bu._REAL_PROFILE_FALLBACK_SENTINEL):
        env.pop(sentinel, None)
    if not (env.get("BU_CDP_URL") or env.get("BU_CDP_WS")):
        return None, ("browser_task needs a browser it can reach over CDP (the local Chromium, `/browser connect`, "
                      "or a CDP cloud provider). Browser Use Cloud direct-API configs are not supported: use "
                      "browser_exec there."), False
    return env, None, bot_desktop


def _bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def run_browser_task(task: str, max_steps: Any = None, timeout_s: Any = None, task_id: Optional[str] = None,
                     python: Optional[str] = None, cdp_env: Optional[dict] = None,
                     model: Optional[dict] = None) -> dict[str, Any]:
    """Run one task; returns the result dict (``status``, ``success``, ``result``, ``steps``, ``cost_usd``, ``error``).
    ``python``, ``cdp_env`` and ``model`` are for tests; the tool resolves them."""
    task = (task or "").strip()
    if not task:
        return {"status": "error", "error": "Say what the browser should do (task)."}
    python = python or browser_use_python()
    if not python:
        return {"status": "error", "error": ("browser-use is not installed for Hermes. Run: uv venv "
                                             "$HERMES_HOME/browser-use && VIRTUAL_ENV=$HERMES_HOME/browser-use uv pip "
                                             "install browser-use==1.0.4")}
    if model is None:
        model, err = resolve_task_model()
        if err:
            return {"status": "error", "error": err}
    bot_desktop = False
    if cdp_env is None:
        cdp_env, err, bot_desktop = _resolve_cdp(task_id)
        if err:
            return {"status": "error", "error": err}
    cfg = _task_cfg()
    timeout = _bounded(timeout_s, _DEFAULT_TIMEOUT_S, 30, _MAX_TIMEOUT_S)
    request = {
        "task": task,
        "cdp_url": cdp_env.get("BU_CDP_URL") or cdp_env.get("BU_CDP_WS"),
        "model": model["model"],
        "base_url": model["base_url"],
        "max_steps": _bounded(max_steps if max_steps is not None else cfg.get("max_steps"), _DEFAULT_MAX_STEPS, 1, 100),
        "timeout": timeout,
        "vision": cfg.get("vision") is not False,
    }
    env = dict(cdp_env)
    # browser-use's venv, not Hermes's: no PYTHONPATH/PYTHONHOME from the harness env.
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["BROWSER_TASK_API_KEY"] = model.get("api_key") or ""
    env["PYTHONUNBUFFERED"] = "1"

    def dispatch() -> dict[str, Any]:
        try:
            proc = subprocess.run([python, "-I", str(_RUNNER)], input=json.dumps(request), capture_output=True,
                                  text=True, env=env, timeout=timeout + 60)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "error": f"browser-use did not finish within {timeout + 60}s"}
        except OSError as e:
            return {"status": "error", "error": f"Could not start browser-use: {e}"}
        return _parse(proc.stdout, proc.returncode, proc.stderr)

    if bot_desktop:
        from tools.browser_session import run_fenced
        return run_fenced({"features": {"local": True}}, dispatch)
    return dispatch()


def _parse(stdout: str, code: int, stderr: str) -> dict[str, Any]:
    steps: list[str] = []
    done: Optional[dict] = None
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "step":
            steps.append(str(event.get("text", ""))[:300])
        elif event.get("type") == "done":
            done = event
    if done is None:
        tail = " ".join((stderr or "").strip().splitlines()[-3:])
        return {"status": "error", "steps": steps, "error": f"browser-use stopped (exit {code}){': ' + tail if tail else ''}"}
    return {
        "status": done.get("status"),
        "success": done.get("success"),
        "result": done.get("text"),
        "steps": steps[-20:],
        "step_count": len(steps),
        "cost_usd": done.get("cost_usd"),
        "error": done.get("error"),
    }


def browser_task(task: str, max_steps: Any = None, timeout_s: Any = None, task_id: Optional[str] = None) -> str:
    from agent.redact import redact_sensitive_text
    from tools.hosted_tool_policy import hosted_tool_refusal
    from tools.registry import tool_error, tool_result
    if refusal := hosted_tool_refusal("browser_task"):
        return tool_error(refusal)
    result = run_browser_task(task, max_steps=max_steps, timeout_s=timeout_s, task_id=task_id)
    if result.get("status") == "error" and not result.get("steps"):
        return tool_error(result.get("error") or "browser_task failed")
    for key in ("result", "error"):
        if isinstance(result.get(key), str):
            result[key] = redact_sensitive_text(result[key], force=True)
    return tool_result(result)


BROWSER_TASK_SCHEMA = {
    "name": "browser_task",
    "description": (
        "Hand a whole browser job to browser-use, a browser agent with its own model, in the same browser as "
        "browser_exec (same tabs and logins). Use it for multi-step web chores where you only need the outcome: "
        "find something, fill a form, collect data from several pages. Say the goal, the site and what to report "
        "back. It returns the answer, whether it succeeded, and the steps it took; the page it ends on stays "
        "open, so browser_exec can continue there. It will not buy, post, send or delete unless the task says so. "
        "For one precise action, use browser_exec instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The goal in plain words, with the site and what to report back."},
            "max_steps": {"type": "integer", "default": _DEFAULT_MAX_STEPS,
                          "description": "Most model steps browser-use may take (1-100)."},
            "timeout_s": {"type": "integer", "default": _DEFAULT_TIMEOUT_S,
                          "description": f"Most seconds the task may run (max {_MAX_TIMEOUT_S})."},
        },
        "required": ["task"],
    },
}


def _handle_browser_task(args: dict, **kw):
    return browser_task(task=args.get("task", ""), max_steps=args.get("max_steps"),
                        timeout_s=args.get("timeout_s"), task_id=kw.get("task_id"))


registry.register(
    name="browser_task",
    toolset="browser-use",
    schema=BROWSER_TASK_SCHEMA,
    handler=_handle_browser_task,
    check_fn=browser_task_available,
    emoji="🧭",
)
