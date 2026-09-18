"""Session-scoped, code-level enforcement for plan mode.

``/plan`` (agent/plan_prompt.py) only ever asked the model nicely, via the
system prompt, not to mutate anything — there was no code-level backstop.
For a smaller/weaker model that's a soft guarantee at best: it can (and in
practice sometimes does) ignore the instruction and call a mutating tool
anyway. This mirrors tools/approval.py's session-scoped YOLO mechanism
(enable/disable/is_enabled, same shape) but for the opposite end of the
spectrum: instead of bypassing approval, it blocks mutating tools outright,
before they ever reach the registry.

Deliberately conservative and small: only the two tool names read_file and
search_files are treated as read-only. Everything else, including terminal,
is blocked while plan mode is active for a session — reliably telling a safe
terminal command ("ls", "git status") apart from a mutating one would mean
parsing command strings, the same class of problem approval.py's dangerous-
command matching already has to work hard at, and getting it wrong in an
ALLOWlist (as opposed to a denylist) fails open in exactly the wrong
direction. A plan-mode session that needs to inspect something not covered
by read_file/search_files should disable plan mode first.
"""

from __future__ import annotations

import threading
from typing import Dict, Any, Optional

from tools.registry import tool_error

_lock = threading.Lock()
_session_plan_mode: set[str] = set()

# Read-only: never changes state outside the conversation.
PLAN_MODE_READ_ONLY_TOOLS = frozenset({"read_file", "search_files"})


def enable_session_plan_mode(session_key: str) -> None:
    """Enable plan-mode enforcement for a single session key."""
    if not session_key:
        return
    with _lock:
        _session_plan_mode.add(session_key)


def disable_session_plan_mode(session_key: str) -> None:
    """Disable plan-mode enforcement for a single session key."""
    if not session_key:
        return
    with _lock:
        _session_plan_mode.discard(session_key)


def is_session_plan_mode_enabled(session_key: str) -> bool:
    if not session_key:
        return False
    with _lock:
        return session_key in _session_plan_mode


def clear_session_plan_mode(session_key: str) -> None:
    """Drop plan-mode state for a session (call alongside approval.clear_session)."""
    if not session_key:
        return
    with _lock:
        _session_plan_mode.discard(session_key)


def maybe_block_for_plan_mode(
    function_name: str, function_args: Dict[str, Any], session_key: Optional[str],
) -> Optional[str]:
    """Returns a JSON tool-error string when plan mode blocks this call
    (same shape as acp_adapter.edit_approval.maybe_require_edit_approval),
    otherwise ``None`` so dispatch can continue."""
    if not session_key or not is_session_plan_mode_enabled(session_key):
        return None
    if function_name in PLAN_MODE_READ_ONLY_TOOLS:
        return None
    return tool_error(
        f"Plan mode is active: \"{function_name}\" would change state outside the conversation, "
        "so it was not run. Only read_file and search_files are available while planning. "
        "Continue building the plan, or ask the user to disable plan mode before making changes."
    )
