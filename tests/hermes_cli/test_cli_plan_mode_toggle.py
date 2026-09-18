"""Regression tests for the CLI ``/plan-mode`` in-chat toggle.

Mirrors test_cli_yolo_toggle.py's approach: _handle_plan_mode_command is a
pure method that only reads self.session_id, so it's tested as an unbound
method against a minimal stand-in rather than constructing a full HermesCLI.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tools.plan_mode_guard as plan_mode_module
from cli import HermesCLI

SESSION_KEY = "test-cli-plan-mode-session"


@pytest.fixture(autouse=True)
def _clear_plan_mode_state():
    plan_mode_module.disable_session_plan_mode(SESSION_KEY)
    plan_mode_module.disable_session_plan_mode("default")
    yield
    plan_mode_module.disable_session_plan_mode(SESSION_KEY)
    plan_mode_module.disable_session_plan_mode("default")


def _make_stand_in(session_id: str = SESSION_KEY) -> SimpleNamespace:
    return SimpleNamespace(session_id=session_id)


class TestTogglePlanModeIsSessionScoped:
    def test_toggle_plan_mode_enables_session_enforcement(self):
        stand_in = _make_stand_in()

        assert plan_mode_module.is_session_plan_mode_enabled(SESSION_KEY) is False

        with patch("cli._cprint"):
            HermesCLI._handle_plan_mode_command(stand_in)

        assert plan_mode_module.is_session_plan_mode_enabled(SESSION_KEY) is True

    def test_toggle_plan_mode_disables_on_second_call(self):
        stand_in = _make_stand_in()
        with patch("cli._cprint"):
            HermesCLI._handle_plan_mode_command(stand_in)  # ON
            assert plan_mode_module.is_session_plan_mode_enabled(SESSION_KEY) is True
            HermesCLI._handle_plan_mode_command(stand_in)  # OFF
            assert plan_mode_module.is_session_plan_mode_enabled(SESSION_KEY) is False

    def test_two_independent_sessions_are_isolated(self):
        cli_a = _make_stand_in(session_id="session-plan-a")
        cli_b = _make_stand_in(session_id="session-plan-b")

        try:
            with patch("cli._cprint"):
                HermesCLI._handle_plan_mode_command(cli_a)

            assert plan_mode_module.is_session_plan_mode_enabled("session-plan-a") is True
            assert plan_mode_module.is_session_plan_mode_enabled("session-plan-b") is False
        finally:
            plan_mode_module.disable_session_plan_mode("session-plan-a")
            plan_mode_module.disable_session_plan_mode("session-plan-b")


class TestPlanModeSlashDispatchRegistration:
    """Regression: a handler existing on the class is not enough — it must
    also actually resolve through _slash_handler (the real dispatch path),
    or (as happened with the gateway /plan-mode command earlier this
    session, and with a first draft of this one manually added to
    _SLASH_DISPATCH against this repo's own convention) it's silently
    unreachable from chat despite existing."""

    def test_plan_mode_not_manually_added_to_dispatch_table(self):
        """OLD_CHAIN_COMMANDS (test_slash_dispatch_table.py) is a frozen
        historical list; a new command should resolve via the
        _handle_<name>_command naming convention, not grow that table."""
        assert "plan-mode" not in HermesCLI._SLASH_DISPATCH

    def test_plan_mode_resolves_via_naming_convention(self):
        resolved = HermesCLI._slash_handler("plan-mode")
        assert resolved is not None
        method_name, _pass_original = resolved
        assert method_name == "_handle_plan_mode_command"
        assert callable(getattr(HermesCLI, method_name))

    def test_plan_mode_resolves_via_central_command_registry(self):
        from hermes_cli.commands import resolve_command

        resolved = resolve_command("plan-mode")
        assert resolved is not None
        assert resolved.name == "plan-mode"
