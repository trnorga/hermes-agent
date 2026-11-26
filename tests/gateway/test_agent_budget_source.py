"""The startup "Agent budget" line names the slot the enforced budget came from.

Why this exists (#108575): two profiles can log the identical ``max_iterations=8`` while being
configured differently — one with ``agent.max_turns: 8`` in ``config.yaml``, the other with no
key at all inheriting ``HERMES_MAX_ITERATIONS`` from ``.env``. ``_bridge_max_turns_to_env``
overwrites the env slot when the config key is present and deletes it when the key is an
explicit ``null``, so the env slot means "config" in the first case and "external" in the
second. A log that prints only the number cannot distinguish them, which is how a profile can
silently drop 8 → 4 when a config write starts setting an explicit key.
"""

import logging

import pytest

from gateway.run import GatewayRunner, _bridge_max_turns_to_env
from hermes_cli.config import TURN_LIMIT_UNLIMITED


def _capture_budget_line(monkeypatch, agent_cfg, env_value: str | None = "90"):
    """Bridge ``agent_cfg`` into the env slot, log the budget, return the log line."""
    monkeypatch.setattr("gateway.run._reload_runtime_env_preserving_config_authority", lambda: None)
    monkeypatch.setattr("gateway.run.get_hermes_home_override", lambda: None)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *a, **k: {"agent": agent_cfg})
    if env_value is None:
        monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)
    else:
        monkeypatch.setenv("HERMES_MAX_ITERATIONS", env_value)
    _bridge_max_turns_to_env(agent_cfg)

    records = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    startup_logger = logging.getLogger("gateway.run")
    startup_logger.addHandler(handler)
    old_level = startup_logger.level
    startup_logger.setLevel(logging.INFO)
    try:
        GatewayRunner._log_agent_budget()
    finally:
        startup_logger.removeHandler(handler)
        startup_logger.setLevel(old_level)
    lines = [r.getMessage() for r in records if "Agent budget" in r.getMessage()]
    assert lines, records
    return lines[0]


def test_a_configured_budget_names_config_yaml(monkeypatch):
    line = _capture_budget_line(monkeypatch, {"max_turns": 50})
    assert "max_iterations=50" in line
    assert "source: agent.max_turns=50 in config.yaml" in line


def test_an_inherited_budget_names_the_env_bridge(monkeypatch):
    """No config key at all: the .env value is what runs, and the line must say so."""
    line = _capture_budget_line(monkeypatch, {}, env_value="90")
    assert "max_iterations=90" in line
    assert "source: HERMES_MAX_ITERATIONS=90" in line
    assert "config.yaml sets no agent.max_turns" in line


def test_config_wins_over_a_differing_env_value(monkeypatch):
    """The bridge overwrites the env slot, so the source must not blame .env."""
    line = _capture_budget_line(monkeypatch, {"max_turns": 12}, env_value="90")
    assert "max_iterations=12" in line
    assert "source: agent.max_turns=12 in config.yaml" in line
    assert "HERMES_MAX_ITERATIONS=90" not in line


def test_unlimited_spelling_survives_in_the_source(monkeypatch):
    line = _capture_budget_line(monkeypatch, {"max_turns": "unlimited"})
    assert "max_iterations=unlimited" in line
    assert TURN_LIMIT_UNLIMITED  # the sentinel the line renders as "unlimited"
    assert "source: agent.max_turns='unlimited' in config.yaml" in line


def test_built_in_default_is_named_when_nothing_sets_it(monkeypatch):
    line = _capture_budget_line(monkeypatch, {}, env_value=None)
    assert "source: built-in default" in line


def test_an_unreadable_config_never_breaks_startup(monkeypatch):
    """The diagnostic is best-effort: a config read error must degrade, not raise."""

    def _boom(*_a, **_k):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("gateway.run._reload_runtime_env_preserving_config_authority", lambda: None)
    monkeypatch.setattr("gateway.run.get_hermes_home_override", lambda: None)
    monkeypatch.setattr("gateway.run._load_gateway_config", _boom)
    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "7")
    assert GatewayRunner._agent_budget_source(7).startswith("unknown (config unreadable")


def test_a_config_value_that_no_longer_matches_is_flagged(monkeypatch):
    """If the env slot was reset after bridging, say the config value is not what runs."""
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *a, **k: {"agent": {"max_turns": 12}})
    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "4")
    source = GatewayRunner._agent_budget_source(4)
    assert "agent.max_turns=12 in config.yaml" in source
    assert "is what the turn loop enforces" in source
