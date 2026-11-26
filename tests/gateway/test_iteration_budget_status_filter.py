"""The iteration-budget notice is DM-only (#79424).

Observed live: a Discord server watched the bot post

    ⚠️ Iteration budget exhausted (4/4) — asking model to summarise

mid-conversation. The turn's real answer arrived two seconds later, so the status was pure
engine plumbing: the counts describe the agent's own tool-call budget, and "asking model to
summarise" describes an internal step. In a DM that line is actionable — the owner raises
``agent.max_turns`` — but every member of a shared chat can only watch the bot narrate its
internals.

The filter reuses ``hidden_outside_dm``, so the chat-type classification (group, channel,
thread, guild, and a ``shared_session`` source keyed ``dm``) is the one already used for the
blocked-overflow warning.
"""

from types import SimpleNamespace

from gateway.run import _prepare_gateway_status_message
from gateway.run_status_filter import (
    ITERATION_BUDGET_STATUS_PATTERN,
    hidden_outside_dm,
)

# The exact strings the two emit sites produce (agent/turn_finalizer.py).
GATEWAY_FORM = "⚠️ Iteration budget exhausted (4/4) — asking model to summarise"
CLI_FORM = "⚠️  Iteration budget exhausted (12/12) — requesting summary..."


def _platform(value: str = "discord"):
    return SimpleNamespace(value=value)


def test_pattern_is_exported_for_callers_that_classify_directly():
    assert "Iteration budget exhausted" in ITERATION_BUDGET_STATUS_PATTERN


def test_shared_chats_drop_the_budget_notice():
    for kind in ("group", "channel", "thread", "guild", "forum"):
        assert hidden_outside_dm(kind, GATEWAY_FORM), kind
        assert hidden_outside_dm(kind, CLI_FORM), kind


def test_a_source_keyed_dm_but_shared_is_not_a_dm():
    """A Slack group DM keys ``dm``; ``status_chat_type`` promotes it to ``group`` upstream."""
    assert hidden_outside_dm("group", GATEWAY_FORM)


def test_dm_keeps_the_budget_notice():
    for kind in ("dm", "private", "direct"):
        assert not hidden_outside_dm(kind, GATEWAY_FORM), kind


def test_unclassified_source_keeps_it():
    """``""`` is an unclassified source: fail open rather than guess the chat is shared."""
    assert not hidden_outside_dm("", GATEWAY_FORM)


def test_a_raised_budget_still_matches():
    assert hidden_outside_dm("group", "⚠️ Iteration budget exhausted (150/150) — asking model to summarise")


def test_ordinary_replies_and_quoted_text_are_untouched():
    for message in (
        "@valex here you go, flat on his belly in silver and black",
        "Iteration budget exhausted — ask Tim to raise max_turns",  # no counts, not an emit
        "the iteration budget exhausted itself on research again",
    ):
        assert not hidden_outside_dm("group", message), message


def test_facade_suppresses_it_on_a_channel():
    """The status callback path (gateway/run.py) must drop it before the platform renders it."""
    assert _prepare_gateway_status_message(_platform(), "lifecycle", GATEWAY_FORM, "channel") is None
    assert _prepare_gateway_status_message(_platform(), "lifecycle", GATEWAY_FORM, "dm") is not None
