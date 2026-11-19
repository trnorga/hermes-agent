"""Tests for the API server listener watchdog.

The failure this guards against: aiohttp's accept loop dies (Windows Proactor
surfaces ``OSError[WinError 64] ERROR_NETNAME_DELETED`` out of
``IocpProactor.accept`` -> "Accept failed on a socket") and asyncio never posts
another accept. The socket stays bound, the process stays alive, TCP handshakes
still complete into the listen backlog, and nothing is ever served.

The distinguishing detail matters: such a listener still *accepts* connections, so
a TCP-only liveness probe reports healthy. Only a probe that waits for bytes can
tell a dead accept loop from a live server.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress

import pytest

from gateway.config import PlatformConfig
from gateway.platforms import api_server
from gateway.platforms.api_server import APIServerAdapter


def _free_port() -> int:
    """A port that is free right now (best-effort; used for the dead-listener case)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _adapter(host: str = "127.0.0.1", port: int = 1) -> APIServerAdapter:
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    adapter._host, adapter._port = host, port
    return adapter


@pytest.mark.asyncio
async def test_probe_false_when_nothing_listens():
    adapter = _adapter(port=_free_port())
    assert await adapter._listener_responds() is False


@pytest.mark.asyncio
async def test_probe_true_against_live_listener():
    async def handle(reader, writer):
        await reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await _adapter(port=port)._listener_responds() is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_false_when_accepts_but_never_answers():
    """The real-world shape: handshake completes, no bytes ever come back."""
    async def black_hole(reader, writer):
        await asyncio.sleep(30)  # accepts, then says nothing

    server = await asyncio.start_server(black_hole, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    adapter = _adapter(port=port)
    original = api_server._LISTENER_PROBE_TIMEOUT
    api_server._LISTENER_PROBE_TIMEOUT = 0.3
    try:
        # A TCP-only check would call this healthy -- that is exactly the bug.
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            pass
        assert await adapter._listener_responds() is False
    finally:
        api_server._LISTENER_PROBE_TIMEOUT = original
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_watchdog_rebinds_after_three_failed_probes(monkeypatch):
    adapter = _adapter()
    adapter._site = object()  # the watchdog only runs with a bound site
    calls = {"probes": 0, "rebinds": 0}

    async def dead_probe():
        calls["probes"] += 1
        return False

    async def fake_rebind():
        calls["rebinds"] += 1
        return True

    async def no_lag():
        return 0.0

    monkeypatch.setattr(api_server, "_LISTENER_PROBE_INTERVAL", 0.01)
    monkeypatch.setattr(api_server, "_LISTENER_FAILURES_BEFORE_REBIND", 3)
    monkeypatch.setattr(adapter, "_listener_responds", dead_probe)
    monkeypatch.setattr(adapter, "_rebind_listener", fake_rebind)
    monkeypatch.setattr(adapter, "_event_loop_lag", no_lag)

    task = asyncio.create_task(adapter._listener_watchdog_loop())
    try:
        for _ in range(300):
            await asyncio.sleep(0.01)
            if calls["rebinds"]:
                break
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert calls["rebinds"] == 1
    assert calls["probes"] >= 3
    assert adapter._listener_failures == 0


@pytest.mark.asyncio
async def test_watchdog_does_not_rebind_a_healthy_listener(monkeypatch):
    adapter = _adapter()
    adapter._site = object()
    calls = {"rebinds": 0}

    async def alive_probe():
        return True

    async def fake_rebind():
        calls["rebinds"] += 1
        return True

    async def no_lag():
        return 0.0

    monkeypatch.setattr(api_server, "_LISTENER_PROBE_INTERVAL", 0.01)
    monkeypatch.setattr(adapter, "_listener_responds", alive_probe)
    monkeypatch.setattr(adapter, "_rebind_listener", fake_rebind)
    monkeypatch.setattr(adapter, "_event_loop_lag", no_lag)

    task = asyncio.create_task(adapter._listener_watchdog_loop())
    await asyncio.sleep(0.2)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert calls["rebinds"] == 0


@pytest.mark.asyncio
async def test_watchdog_skips_probe_while_event_loop_is_starved(monkeypatch):
    """A busy loop cannot answer the probe either -- that is not a dead listener."""
    adapter = _adapter()
    adapter._site = object()
    calls = {"probes": 0, "rebinds": 0}

    async def dead_probe():
        calls["probes"] += 1
        return False

    async def starved():
        return 10.0

    async def fake_rebind():
        calls["rebinds"] += 1
        return True

    monkeypatch.setattr(api_server, "_LISTENER_PROBE_INTERVAL", 0.01)
    monkeypatch.setattr(adapter, "_listener_responds", dead_probe)
    monkeypatch.setattr(adapter, "_rebind_listener", fake_rebind)
    monkeypatch.setattr(adapter, "_event_loop_lag", starved)

    task = asyncio.create_task(adapter._listener_watchdog_loop())
    await asyncio.sleep(0.15)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert calls["rebinds"] == 0
    assert calls["probes"] == 0


@pytest.mark.asyncio
async def test_event_loop_lag_is_small_on_idle_loop():
    assert await APIServerAdapter._event_loop_lag() < 1.0
