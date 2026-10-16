"""SupportFileManager: ask the controller for a support file, wait for it, save it.

The connection is a recording stand-in. Device status and generation go through
``request`` exactly as aiounifi's ``ApiRequestV2.decode`` returns them (one dict
wrapped in a list); downloads go through ``session_download`` and the console's
generate through ``session_post``. Sleeping advances a fake clock, so the
timeouts are exercised without waiting.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from aiounifi.models.api import ApiRequestV2
from unifi_core.network.managers.connection_manager import SessionDownload
from unifi_core.network.managers.support_file_manager import SupportFile, SupportFileManager, support_file_path

MAC = "aa:bb:cc:dd:ee:ff"
DEVICE_DOWNLOAD = f"/proxy/network/v2/api/site/default/support/{MAC}/download"
CONSOLE_DOWNLOAD = "/api/support/file/download"


class Connection:
    site = "default"

    def __init__(self, statuses: list[str] | None = None, downloads: list[int] | None = None) -> None:
        self.statuses = list(statuses or [])
        self.downloads = list(downloads or [200])
        self.calls: list[tuple[str, str]] = []
        self.posted: list[tuple[str, Any]] = []

    def network_path(self, api_request: ApiRequestV2) -> str:
        return api_request.full_path(self.site, True)

    async def request(self, api_request: ApiRequestV2) -> Any:
        assert isinstance(api_request, ApiRequestV2)
        self.calls.append((api_request.method, api_request.path))
        if api_request.method == "get":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return [{"mac": MAC, "status": status}]
        return [{}]

    async def session_post(self, path: str, payload: Any) -> int:
        self.calls.append(("post", path))
        self.posted.append((path, payload))
        return 200

    async def session_download(self, path: str, dest: Path) -> SessionDownload:
        self.calls.append(("download", path))
        status = self.downloads.pop(0) if len(self.downloads) > 1 else self.downloads[0]
        if status != 200:
            return SessionDownload(status=status, size_bytes=0, filename=None)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x1f\x8bfixture")
        return SessionDownload(status=200, size_bytes=9, filename="fixture.tgz")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert len(self.sleeps) < 1000, "polled without ever giving up"
        self.sleeps.append(seconds)
        self.now += seconds


def manager(connection: Connection, clock: Clock, *, timeout: float = 60) -> SupportFileManager:
    return SupportFileManager(connection, sleep=clock.sleep, clock=clock, poll_interval=5, timeout=timeout)


# ---- device ----


async def test_a_device_file_is_generated_waited_for_and_downloaded(tmp_path) -> None:
    connection = Connection(statuses=["READY_TO_GENERATE", "GENERATING", "GENERATING", "READY_TO_DOWNLOAD"])
    clock = Clock()
    dest = tmp_path / "ap.tgz"

    result = await manager(connection, clock).generate_device_support_file(MAC, dest)

    assert result == SupportFile(path=dest, size_bytes=9)
    assert connection.calls == [
        ("get", f"/support/{MAC}"),
        ("post", f"/support/{MAC}/generation"),
        ("get", f"/support/{MAC}"),
        ("get", f"/support/{MAC}"),
        ("get", f"/support/{MAC}"),
        ("download", DEVICE_DOWNLOAD),
    ]
    assert clock.sleeps == [5, 5, 5]


async def test_the_mac_is_sent_in_canonical_form(tmp_path) -> None:
    connection = Connection(statuses=["READY_TO_GENERATE", "READY_TO_DOWNLOAD"])

    await manager(connection, Clock()).generate_device_support_file("AA-BB-CC-DD-EE-FF", tmp_path / "ap.tgz")

    assert connection.calls[0] == ("get", f"/support/{MAC}")
    assert connection.calls[-1] == ("download", DEVICE_DOWNLOAD)


async def test_a_generation_already_running_is_waited_for_not_restarted(tmp_path) -> None:
    connection = Connection(statuses=["GENERATING", "READY_TO_DOWNLOAD"])

    await manager(connection, Clock()).generate_device_support_file(MAC, tmp_path / "ap.tgz")

    assert ("post", f"/support/{MAC}/generation") not in connection.calls


async def test_a_file_already_ready_is_downloaded_without_generating(tmp_path) -> None:
    connection = Connection(statuses=["READY_TO_DOWNLOAD"])

    await manager(connection, Clock()).generate_device_support_file(MAC, tmp_path / "ap.tgz")

    assert connection.calls == [("get", f"/support/{MAC}"), ("download", DEVICE_DOWNLOAD)]


async def test_a_device_that_never_finishes_times_out_without_downloading(tmp_path) -> None:
    connection = Connection(statuses=["READY_TO_GENERATE", "GENERATING"])
    clock = Clock()

    with pytest.raises(TimeoutError, match="60 seconds"):
        await manager(connection, clock, timeout=60).generate_device_support_file(MAC, tmp_path / "ap.tgz")

    assert not any(call[0] == "download" for call in connection.calls)
    assert clock.now <= 60
    assert not (tmp_path / "ap.tgz").exists()


async def test_a_file_taken_by_another_download_is_reported(tmp_path) -> None:
    connection = Connection(statuses=["READY_TO_DOWNLOAD"], downloads=[400])

    with pytest.raises(RuntimeError, match="no longer available"):
        await manager(connection, Clock()).generate_device_support_file(MAC, tmp_path / "ap.tgz")


async def test_an_unexpected_device_status_is_an_error_not_a_wait(tmp_path) -> None:
    connection = Connection(statuses=["SOMETHING_NEW"])

    with pytest.raises(RuntimeError, match="SOMETHING_NEW"):
        await manager(connection, Clock()).generate_device_support_file(MAC, tmp_path / "ap.tgz")
    assert len(connection.calls) == 1


@pytest.mark.parametrize("mac", ["", "not-a-mac", None, "aa:bb:cc:dd:ee"])
async def test_a_value_that_is_not_a_mac_is_refused_before_any_request(tmp_path, mac) -> None:
    connection = Connection(statuses=["READY_TO_GENERATE"])

    with pytest.raises(ValueError):
        await manager(connection, Clock()).generate_device_support_file(mac, tmp_path / "ap.tgz")
    assert connection.calls == []


# ---- console ----


async def test_the_console_file_is_generated_then_downloaded_once_ready(tmp_path) -> None:
    connection = Connection(downloads=[423, 423, 200])
    clock = Clock()
    dest = tmp_path / "console.tgz"

    result = await manager(connection, clock).generate_console_support_file(dest)

    assert result == SupportFile(path=dest, size_bytes=9)
    assert connection.posted == [("/api/support/file/generate", {"recreate": True})]
    assert connection.calls == [
        ("post", "/api/support/file/generate"),
        ("download", CONSOLE_DOWNLOAD),
        ("download", CONSOLE_DOWNLOAD),
        ("download", CONSOLE_DOWNLOAD),
    ]
    assert clock.sleeps == [5, 5]


async def test_a_console_file_that_never_finishes_times_out(tmp_path) -> None:
    connection = Connection(downloads=[423])
    clock = Clock()

    with pytest.raises(TimeoutError, match="60 seconds"):
        await manager(connection, clock, timeout=60).generate_console_support_file(tmp_path / "console.tgz")
    assert clock.now <= 60


async def test_a_console_download_refused_for_another_reason_is_an_error(tmp_path) -> None:
    connection = Connection(downloads=[500])

    with pytest.raises(RuntimeError, match="500"):
        await manager(connection, Clock()).generate_console_support_file(tmp_path / "console.tgz")
    assert [call[0] for call in connection.calls] == ["post", "download"]


async def test_a_refused_console_generate_is_an_error(tmp_path) -> None:
    connection = Connection()

    async def refused(path: str, payload: Any) -> int:
        connection.calls.append(("post", path))
        return 403

    connection.session_post = refused  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="403"):
        await manager(connection, Clock()).generate_console_support_file(tmp_path / "console.tgz")
    assert connection.calls == [("post", "/api/support/file/generate")]


async def test_a_device_download_failing_for_another_reason_reports_its_status(tmp_path) -> None:
    connection = Connection(statuses=["READY_TO_DOWNLOAD"], downloads=[502])

    with pytest.raises(RuntimeError, match="HTTP 502") as raised:
        await manager(connection, Clock()).generate_device_support_file(MAC, tmp_path / "ap.tgz")
    assert "only once" not in str(raised.value)


# ---- naming ----

PDT = dt.timezone(dt.timedelta(hours=-7), "PDT")
WHEN = dt.datetime(2026, 10, 16, 6, 19, 42, tzinfo=PDT)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("U7-Pro-Wall-Kitchen", "U7-Pro-Wall-Kitchen-support-2026-10-16-0619-PDT.tgz"),
        ("USW Flex 2.5G 8 PoE - Garage", "USW-Flex-2.5G-8-PoE-Garage-support-2026-10-16-0619-PDT.tgz"),
        ("../../etc/passwd", "etc-passwd-support-2026-10-16-0619-PDT.tgz"),
        ("  ", "unifi-support-2026-10-16-0619-PDT.tgz"),
        (None, "unifi-support-2026-10-16-0619-PDT.tgz"),
    ],
)
def test_the_file_name_says_which_device_and_when(tmp_path, label, expected) -> None:
    assert support_file_path(tmp_path, label, WHEN) == tmp_path / expected


def test_an_existing_file_is_never_overwritten(tmp_path) -> None:
    first = support_file_path(tmp_path, "AP", WHEN)
    first.write_bytes(b"earlier")
    second = support_file_path(tmp_path, "AP", WHEN)
    second.write_bytes(b"later")

    assert second == tmp_path / "AP-support-2026-10-16-0619-PDT-2.tgz"
    assert support_file_path(tmp_path, "AP", WHEN) == tmp_path / "AP-support-2026-10-16-0619-PDT-3.tgz"
    assert first.read_bytes() == b"earlier"
