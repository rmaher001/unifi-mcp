"""The bootstrap is re-fetched while the connection lives.

``initialize()`` fetched the bootstrap once and nothing fetched it again, so
every field the websocket does not push stayed at its value from connect
time. After a console's UniFi OS update and reboot, the NVR still read
``firmware_version`` of the old release, ``is_updating: true`` and the
previous ``up_since`` until the process restarted, while the console itself
reported the new version. A periodic ``client.update()`` (what Home
Assistant's Protect integration does) bounds that staleness.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from unifi_core.protect.managers import connection_manager as module


def _client():
    client = MagicMock()
    client.update = AsyncMock()
    client.async_disconnect_ws = AsyncMock()
    client.close_session = AsyncMock()
    return client


async def _connected(monkeypatch, client, **kwargs):
    monkeypatch.setattr(module, "ProtectApiClient", MagicMock(return_value=client))
    manager = module.ProtectConnectionManager(host="controller.invalid", username="u", password="p", **kwargs)
    assert await manager.initialize() is True
    return manager


@pytest.mark.asyncio
async def test_the_bootstrap_is_fetched_again_on_an_interval(monkeypatch):
    client = _client()
    manager = await _connected(monkeypatch, client, bootstrap_refresh_seconds=0.01)
    try:
        await asyncio.sleep(0.05)
        assert client.update.await_count >= 3, "the connect fetch, then at least two refreshes"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_connection_and_tries_again(monkeypatch):
    client = _client()
    manager = await _connected(monkeypatch, client, bootstrap_refresh_seconds=0.01)
    client.update.side_effect = RuntimeError("console rebooting")
    try:
        await asyncio.sleep(0.05)
        assert client.update.await_count >= 3, "a failure does not end the refreshing"
        assert manager._client is client and manager._initialized
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_close_stops_the_refreshing(monkeypatch):
    client = _client()
    manager = await _connected(monkeypatch, client, bootstrap_refresh_seconds=0.01)
    await manager.close()
    count = client.update.await_count
    await asyncio.sleep(0.05)
    assert client.update.await_count == count


@pytest.mark.asyncio
async def test_zero_turns_the_refreshing_off(monkeypatch):
    client = _client()
    manager = await _connected(monkeypatch, client, bootstrap_refresh_seconds=0)
    try:
        await asyncio.sleep(0.03)
        assert client.update.await_count == 1, "only the connect fetch"
    finally:
        await manager.close()


def test_the_default_interval_is_a_minute():
    manager = module.ProtectConnectionManager(host="controller.invalid", username="u", password="p")
    assert manager._bootstrap_refresh_seconds == 60.0
