"""Tests for the unifi_generate_support_file tool.

Tool-layer behaviour: input validation, preview/confirm, where the file is
saved and what the response says. The controller exchange itself is covered
by unifi-core's SupportFileManager tests.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from unifi_core.exceptions import UniFiNotFoundError
from unifi_core.network.managers.support_file_manager import SupportFile

os.environ.setdefault("UNIFI_HOST", "127.0.0.1")
os.environ.setdefault("UNIFI_USERNAME", "test")
os.environ.setdefault("UNIFI_PASSWORD", "test")

MAC = "aa:bb:cc:dd:ee:ff"
MODULE = "unifi_network_mcp.tools.system"


@pytest.fixture
def mocks(tmp_path):
    support = SimpleNamespace(
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
    assert "generated_at" in data
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

    assert set(result["data"]) == {"target", "name", "path", "size_bytes", "generated_at"}


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


def test_the_default_folder_is_documented_in_config() -> None:
    from unifi_network_mcp.bootstrap import load_config

    assert load_config().network.support_files.directory == "~/unifi-support-files"
