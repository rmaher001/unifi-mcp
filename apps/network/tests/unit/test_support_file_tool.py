"""Tests for the unifi_generate_support_file tool.

Tool-layer behaviour: input validation, preview/confirm, where the file is
saved and what the response says. The controller exchange itself is covered
by unifi-core's SupportFileManager tests.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from unifi_core.exceptions import UniFiAuthError, UniFiNotFoundError
from unifi_core.network.managers.support_file_manager import SupportFile

os.environ.setdefault("UNIFI_HOST", "127.0.0.1")
os.environ.setdefault("UNIFI_USERNAME", "test")
os.environ.setdefault("UNIFI_PASSWORD", "test")

MAC = "aa:bb:cc:dd:ee:ff"
MODULE = "unifi_network_mcp.tools.system"


@pytest.fixture
def mocks(tmp_path):
    support = SimpleNamespace(
        require_session_auth=MagicMock(),
        generate_device_support_file=AsyncMock(
            side_effect=lambda mac_address, dest: _saved(dest, 2_983_877),
        ),
        generate_console_support_file=AsyncMock(
            side_effect=lambda dest: _saved(dest, 55_051_454),
        ),
    )
    devices = SimpleNamespace(
        get_device_details=AsyncMock(return_value=SimpleNamespace(name="Kitchen AP", mac=MAC, model="U7PW")),
    )
    system = SimpleNamespace(get_system_info=AsyncMock(return_value={"name": "Network", "hostname": "Console One"}))
    with (
        patch(f"{MODULE}.support_file_manager", support),
        patch(f"{MODULE}.device_manager", devices),
        patch(f"{MODULE}.system_manager", system),
        patch(f"{MODULE}.support_file_directory", return_value=tmp_path / "files"),
    ):
        yield SimpleNamespace(support=support, devices=devices, system=system, directory=tmp_path / "files")


def _saved(dest: Path, size: int) -> SupportFile:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"\x1f\x8b")
    return SupportFile(path=dest, size_bytes=size)


async def test_a_device_preview_names_the_device_and_folder_without_generating(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    result = await generate_support_file(target="device", mac_address=MAC)

    assert result["success"] is True
    assert result["requires_confirmation"] is True
    preview = str(result["preview"])
    assert "Kitchen AP" in preview
    assert str(mocks.directory) in preview
    mocks.support.generate_device_support_file.assert_not_awaited()


async def test_a_confirmed_device_file_is_saved_under_the_device_name(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    result = await generate_support_file(target="device", mac_address=MAC, confirm=True)

    assert result["success"] is True
    data = result["data"]
    path = Path(data["path"])
    assert path.parent == mocks.directory
    assert path.name.startswith("Kitchen-AP-support-") and path.suffix == ".tgz"
    assert data["size_bytes"] == 2_983_877
    assert data["target"] == "device"
    assert data["name"] == "Kitchen AP"
    assert data["mac"] == MAC
    assert "requested_at" in data
    mocks.support.generate_device_support_file.assert_awaited_once_with(MAC, path)


async def test_a_confirmed_console_file_is_saved_under_the_console_name(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    result = await generate_support_file(target="console", confirm=True)

    assert result["success"] is True
    path = Path(result["data"]["path"])
    assert path.name.startswith("Console-One-support-")
    assert result["data"]["size_bytes"] == 55_051_454
    assert "mac" not in result["data"]
    mocks.support.generate_console_support_file.assert_awaited_once_with(path)


async def test_the_response_never_carries_the_file_contents(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    result = await generate_support_file(target="console", confirm=True)

    assert set(result["data"]) == {"target", "name", "path", "size_bytes", "requested_at"}


async def test_a_device_target_needs_a_mac(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    result = await generate_support_file(target="device", confirm=True)

    assert result["success"] is False
    assert "mac" in result["error"].lower()
    mocks.support.generate_device_support_file.assert_not_awaited()


async def test_an_unknown_device_is_reported_without_generating(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    mocks.devices.get_device_details.side_effect = UniFiNotFoundError("device", MAC)

    result = await generate_support_file(target="device", mac_address=MAC, confirm=True)

    assert result["success"] is False
    assert "Failed to generate support file" in result["error"]
    mocks.support.generate_device_support_file.assert_not_awaited()


async def test_a_timeout_is_reported_as_a_failed_generation(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    mocks.support.generate_console_support_file.side_effect = TimeoutError(
        "The support file was not ready within 600 seconds."
    )

    result = await generate_support_file(target="console", confirm=True)

    assert result["success"] is False
    assert "600 seconds" in result["error"]


async def test_an_unknown_target_is_refused(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    result = await generate_support_file(target="nas", confirm=True)

    assert result["success"] is False
    assert "device" in result["error"] and "console" in result["error"]


def test_the_folder_comes_from_config_and_expands_home(monkeypatch) -> None:
    from unifi_network_mcp.tools import system

    monkeypatch.setattr(
        system,
        "config",
        SimpleNamespace(network=SimpleNamespace(support_files=SimpleNamespace(directory="~/unifi-support-files"))),
    )

    assert system.support_file_directory() == Path.home() / "unifi-support-files"


def test_the_default_folder_is_documented_in_config(monkeypatch) -> None:
    from unifi_network_mcp.bootstrap import load_config

    monkeypatch.delenv("UNIFI_NETWORK_SUPPORT_FILE_DIR", raising=False)

    assert load_config().network.support_files.directory == "~/unifi-support-files"


async def test_a_console_preview_names_the_console_without_generating(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    result = await generate_support_file(target="console")

    assert result["requires_confirmation"] is True
    assert "Console One" in str(result["preview"])
    assert "mac" not in str(result["preview"])
    mocks.support.generate_console_support_file.assert_not_awaited()


async def test_the_mac_is_shown_and_sent_in_canonical_form(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    preview = await generate_support_file(target="device", mac_address="AA-BB-CC-DD-EE-FF")
    result = await generate_support_file(target="device", mac_address="AA-BB-CC-DD-EE-FF", confirm=True)

    assert MAC in str(preview["preview"])
    assert result["data"]["mac"] == MAC
    mocks.devices.get_device_details.assert_awaited_with(MAC)
    assert mocks.support.generate_device_support_file.await_args.args[0] == MAC


async def test_a_malformed_mac_is_refused_before_any_lookup(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    result = await generate_support_file(target="device", mac_address="not-a-mac")

    assert result["success"] is False
    assert "mac" in result["error"].lower()
    mocks.devices.get_device_details.assert_not_awaited()


async def test_a_preview_already_says_when_session_credentials_are_missing(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    mocks.support.require_session_auth.side_effect = UniFiAuthError("requires Network session authentication")

    result = await generate_support_file(target="console")

    assert result["success"] is False
    assert "session authentication" in result["error"]


async def test_a_nameless_device_or_console_still_gets_a_telling_name(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    mocks.devices.get_device_details.return_value = SimpleNamespace(name="", mac=MAC)
    mocks.system.get_system_info.return_value = {"name": "Network Console"}
    device = await generate_support_file(target="device", mac_address=MAC, confirm=True)
    console = await generate_support_file(target="console", confirm=True)
    mocks.system.get_system_info.return_value = {}
    bare = await generate_support_file(target="console", confirm=True)

    assert device["data"]["name"] == MAC
    assert console["data"]["name"] == "Network Console"
    assert bare["data"]["name"] == "console"


@pytest.fixture
def pacific(monkeypatch):
    """Run in a known local zone, as the server's own clock would be."""
    import time

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


async def test_the_file_name_carries_the_local_date_time_and_zone(mocks, pacific) -> None:
    from datetime import datetime, timedelta

    from unifi_network_mcp.tools.system import generate_support_file

    local = datetime(2026, 10, 16, 6, 19, 42)  # naive local time, as datetime.now() returns
    reads = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            reads.append(None)
            return local + timedelta(minutes=len(reads) - 1)  # a minute later on every read

    with patch(f"{MODULE}.datetime", Clock):
        result = await generate_support_file(target="console", confirm=True)

    assert Path(result["data"]["path"]).name == "Console-One-support-2026-10-16-0619-PDT.tgz"
    assert result["data"]["requested_at"] == "2026-10-16T06:19:42-07:00"


async def test_a_failed_generation_leaves_no_empty_file_behind(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    mocks.support.generate_console_support_file.side_effect = TimeoutError("not ready within 600 seconds")

    await generate_support_file(target="console", confirm=True)

    assert list(mocks.directory.iterdir()) == []


async def test_failures_are_logged_by_exception_class_only(mocks, caplog) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    mocks.support.generate_console_support_file.side_effect = RuntimeError("controller text aa:bb:cc:dd:ee:ff")

    with caplog.at_level("DEBUG"):
        await generate_support_file(target="console", confirm=True)

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged
    assert "aa:bb:cc:dd:ee:ff" not in logged
    assert not any(record.exc_info for record in caplog.records)


def test_the_folder_can_be_set_by_environment(monkeypatch) -> None:
    from unifi_network_mcp.bootstrap import load_config

    monkeypatch.setenv("UNIFI_NETWORK_SUPPORT_FILE_DIR", "/srv/support")

    assert load_config().network.support_files.directory == "/srv/support"


@pytest.mark.parametrize("value", ["", "   ", "relative/folder"])
def test_an_empty_or_relative_folder_is_refused(monkeypatch, value) -> None:
    from unifi_network_mcp.tools import system

    monkeypatch.setattr(
        system, "config", SimpleNamespace(network=SimpleNamespace(support_files=SimpleNamespace(directory=value)))
    )

    with pytest.raises(ValueError, match="UNIFI_NETWORK_SUPPORT_FILE_DIR"):
        system.support_file_directory()


async def test_a_cancelled_call_leaves_no_empty_file_behind(mocks) -> None:
    import asyncio

    from unifi_network_mcp.tools.system import generate_support_file

    mocks.support.generate_console_support_file.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await generate_support_file(target="console", confirm=True)
    assert list(mocks.directory.iterdir()) == []


async def test_a_file_that_already_holds_data_is_never_removed(mocks) -> None:
    from unifi_network_mcp.tools.system import generate_support_file

    def written_then_failed(dest):
        dest.write_bytes(b"\x1f\x8bpartly checked")
        raise RuntimeError("late failure")

    mocks.support.generate_console_support_file.side_effect = written_then_failed

    result = await generate_support_file(target="console", confirm=True)

    assert result["success"] is False
    assert len(list(mocks.directory.iterdir())) == 1


def test_surrounding_spaces_in_the_folder_setting_are_ignored(monkeypatch) -> None:
    from unifi_network_mcp.tools import system

    monkeypatch.setattr(
        system,
        "config",
        SimpleNamespace(network=SimpleNamespace(support_files=SimpleNamespace(directory="  /srv/support  "))),
    )

    assert system.support_file_directory() == Path("/srv/support")


def test_the_tool_is_gated_as_a_system_create(monkeypatch) -> None:
    import json

    from unifi_core.policy_gate import PolicyGateChecker
    from unifi_network_mcp.categories import NETWORK_CATEGORY_MAP

    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "src/unifi_network_mcp/tools_manifest.json").read_text()
    )
    entry = next(tool for tool in manifest["tools"] if tool["name"] == "unifi_generate_support_file")
    assert (entry["permission_category"], entry["permission_action"]) == ("system", "create")

    monkeypatch.setenv("UNIFI_POLICY_NETWORK_SYSTEM_CREATE", "false")
    checker = PolicyGateChecker("network", NETWORK_CATEGORY_MAP)
    assert checker.check("system", "create") is False
