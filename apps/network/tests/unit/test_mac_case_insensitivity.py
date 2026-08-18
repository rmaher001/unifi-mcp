"""MAC lookups must be case-insensitive at every entry point.

The controller stores and reports MACs in lowercase. Every one of these
lookups previously compared the caller's string to the controller's with a
raw `==`, so an uppercase MAC - the form printed on most device labels -
reported "not found" for hardware that was sitting right there. One code
path (StatsManager.get_client_wifi_details) already lowercased both sides,
which is what made the inconsistency invisible: whichever tool you happened
to try first decided whether you believed the bug existed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from unifi_core.network.managers.client_manager import ClientManager
from unifi_core.network.managers.device_manager import DeviceManager
from unifi_core.network.managers.event_manager import EventBuffer

LOWER = "aa:bb:cc:dd:ee:ff"
UPPER = "AA:BB:CC:DD:EE:FF"


@pytest.fixture
def mock_connection():
    conn = MagicMock()
    conn.site = "default"
    conn.get_cached = MagicMock(return_value=None)
    conn._update_cache = MagicMock()
    conn._invalidate_cache = MagicMock()
    conn.ensure_connected = AsyncMock(return_value=True)
    conn.controller = MagicMock()
    conn.controller.clients = MagicMock()
    conn.controller.clients.update = AsyncMock()
    conn.controller.clients.values = MagicMock(return_value=[])
    conn.controller.clients_all = MagicMock()
    conn.controller.clients_all.update = AsyncMock()
    conn.controller.clients_all.values = MagicMock(return_value=[])
    conn.controller.devices = MagicMock()
    conn.controller.devices.update = AsyncMock()
    conn.controller.devices.values = MagicMock(return_value=[])
    conn.request = AsyncMock(return_value=None)
    return conn


def _client(mac: str, **extra):
    obj = MagicMock()
    obj.mac = mac
    obj.raw = {"mac": mac, **extra}
    return obj


@pytest.mark.asyncio
async def test_client_lookup_accepts_uppercase(mock_connection):
    """ClientManager.get_client_details backs block/unblock/rename/forget/
    reconnect/authorize/set-ip - one raw `==` broke all of them at once."""
    mock_connection.controller.clients.values.return_value = [_client(LOWER, signal=-52)]
    mgr = ClientManager(mock_connection)
    result = await mgr.get_client_details(UPPER)
    assert result.raw["signal"] == -52


@pytest.mark.asyncio
async def test_client_lookup_still_rejects_a_genuinely_absent_mac(mock_connection):
    """Normalizing must not turn the lookup into a match-anything."""
    from unifi_core.exceptions import UniFiNotFoundError

    mock_connection.controller.clients.values.return_value = [_client(LOWER)]
    mgr = ClientManager(mock_connection)
    with pytest.raises(UniFiNotFoundError):
        await mgr.get_client_details("11:22:33:44:55:66")


@pytest.mark.asyncio
async def test_device_lookup_accepts_uppercase(mock_connection):
    """DeviceManager.get_device_details backs reboot/adopt/upgrade/rename/
    radio/PDU-outlet."""
    device = MagicMock()
    device.mac = LOWER
    mock_connection.controller.devices.values.return_value = [device]
    mgr = DeviceManager(mock_connection)
    assert await mgr.get_device_details(UPPER) is device


@pytest.mark.asyncio
async def test_device_lookup_still_raises_for_absent_mac(mock_connection):
    from unifi_core.exceptions import UniFiNotFoundError

    device = MagicMock()
    device.mac = LOWER
    mock_connection.controller.devices.values.return_value = [device]
    mgr = DeviceManager(mock_connection)
    with pytest.raises(UniFiNotFoundError):
        await mgr.get_device_details("11:22:33:44:55:66")


def test_event_buffer_mac_filter_accepts_uppercase() -> None:
    """The websocket buffer stores controller-cased MACs; an uppercase filter
    returned zero matches with no error to suggest why."""
    buf = EventBuffer(max_size=10, ttl_seconds=300)
    buf.add({"id": "e1", "mac": LOWER})
    buf.add({"id": "e2", "mac": "11:22:33:44:55:66"})
    assert [e["id"] for e in buf.get_recent(mac=UPPER)] == ["e1"]


def test_event_buffer_mac_filter_excludes_other_macs() -> None:
    buf = EventBuffer(max_size=10, ttl_seconds=300)
    buf.add({"id": "e1", "mac": LOWER})
    assert buf.get_recent(mac="11:22:33:44:55:66") == []


def test_event_buffer_mac_filter_skips_events_without_a_mac() -> None:
    """An event carrying no `mac` must not match a MAC filter."""
    buf = EventBuffer(max_size=10, ttl_seconds=300)
    buf.add({"id": "no-mac"})
    assert buf.get_recent(mac=UPPER) == []
