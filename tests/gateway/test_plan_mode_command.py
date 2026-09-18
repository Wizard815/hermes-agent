"""Tests for gateway /plan-mode session scoping."""

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from tools.plan_mode_guard import disable_session_plan_mode, is_session_plan_mode_enabled


@pytest.fixture(autouse=True)
def _clean_plan_mode_state():
    disable_session_plan_mode("agent:main:telegram:dm:plan-a")
    disable_session_plan_mode("agent:main:telegram:dm:plan-b")
    yield
    disable_session_plan_mode("agent:main:telegram:dm:plan-a")
    disable_session_plan_mode("agent:main:telegram:dm:plan-b")


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.session_store = None
    runner.config = None
    return runner


def _make_event(chat_id: str) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=f"user-{chat_id}",
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
    )
    return MessageEvent(text="/plan-mode", source=source)


@pytest.mark.asyncio
async def test_plan_mode_command_toggles_only_current_session():
    runner = _make_runner()

    event_a = _make_event("plan-a")
    session_a = runner._session_key_for_source(event_a.source)
    session_b = runner._session_key_for_source(_make_event("plan-b").source)

    result_on = await runner._handle_plan_mode_command(event_a)

    assert "ON" in str(result_on)
    assert is_session_plan_mode_enabled(session_a) is True
    assert is_session_plan_mode_enabled(session_b) is False

    result_off = await runner._handle_plan_mode_command(event_a)

    assert "OFF" in str(result_off)
    assert is_session_plan_mode_enabled(session_a) is False


def test_plan_mode_registered_in_idle_command_table():
    """Regression: the slash name -> handler mapping (run_busy._IDLE_COMMANDS)
    must actually list "plan-mode", or the command is unreachable from chat
    even though the handler method exists."""
    from gateway.run_busy import GatewayBusySessionMixin

    assert "plan-mode" in GatewayBusySessionMixin._IDLE_COMMANDS
    runner = _make_runner()
    table = runner._command_handler_table(GatewayBusySessionMixin._IDLE_COMMANDS)
    assert table["plan-mode"] == runner._handle_plan_mode_command
