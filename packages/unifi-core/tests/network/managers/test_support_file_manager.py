"""SupportFileManager: ask the controller for a support file, wait for it, save it.

The connection is a recording stand-in. Device status and generation go through
``request`` exactly as aiounifi's ``ApiRequestV2.decode`` returns them (one dict
wrapped in a list); downloads go through ``session_download`` and the console's
generate through ``session_post``. Sleeping advances a fake clock, so the
timeouts are exercised without waiting.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiounifi.models.api import ApiRequestV2
from unifi_core.network.managers.connection_manager import SessionDownload
from unifi_core.network.managers.support_file_manager import SupportFile, SupportFileManager, reserve_support_file

MAC = "aa:bb:cc:dd:ee:ff"
GZIP_BODY = b"\x1f\x8bfixture"
DEVICE_DOWNLOAD = f"/proxy/network/v2/api/site/default/support/{MAC}/download"
CONSOLE_DOWNLOAD = "/api/support/file/download"


class Connection:
    site = "default"
    is_unifi_os = True

    def __init__(
        self, statuses: list[str] | None = None, downloads: list[int] | None = None, body: bytes = GZIP_BODY
    ) -> None:
        self.statuses = list(statuses or [])
        self.downloads = list(downloads or [200])
        self.body = body
        self.authentication_status = SimpleNamespace(session_configured=True, api_key_available=None)
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[Any] = []
        self.posted: list[tuple[str, Any]] = []

    def network_path(self, api_request: ApiRequestV2) -> str:
        return api_request.full_path(self.site, True)

    async def ensure_connected(self) -> bool:
        return True

    async def request(self, api_request: ApiRequestV2) -> Any:
        assert isinstance(api_request, ApiRequestV2)
        self.calls.append((api_request.method, api_request.path))
        self.bodies.append(api_request.data)
        if api_request.method == "get":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return status if not isinstance(status, str) else [{"mac": MAC, "status": status}]
        return [{}]

    async def session_post(self, path: str, payload: Any) -> int:
        self.calls.append(("post", path))
        self.posted.append((path, payload))
        return 200

    async def session_download(self, path: str, dest: Path) -> SessionDownload:
        self.calls.append(("download", path))
        status = self.downloads.pop(0) if len(self.downloads) > 1 else self.downloads[0]
        if status != 200:
            return SessionDownload(status=status, size_bytes=0)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.body)
        return SessionDownload(status=200, size_bytes=len(self.body))


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
        await asyncio.sleep(0)  # yield like a real sleep, so concurrent callers interleave


def manager(connection: Connection, clock: Clock, *, timeout: float = 60) -> SupportFileManager:
    return SupportFileManager(connection, sleep=clock.sleep, clock=clock, poll_interval=5, timeout=timeout)


# ---- device ----


async def test_a_device_file_is_generated_waited_for_and_downloaded(tmp_path) -> None:
    connection = Connection(statuses=["READY_TO_GENERATE", "GENERATING", "GENERATING", "READY_TO_DOWNLOAD"])
    clock = Clock()
    dest = tmp_path / "ap.tgz"

    result = await manager(connection, clock).generate_device_support_file(MAC, dest)

    assert result == SupportFile(path=dest, size_bytes=len(GZIP_BODY))
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

    assert result == SupportFile(path=dest, size_bytes=len(GZIP_BODY))
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
        ("Küche AP", "Küche-AP-support-2026-10-16-0619-PDT.tgz"),
        ("客厅", "客厅-support-2026-10-16-0619-PDT.tgz"),
        ("A" * 300, "A" * 64 + "-support-2026-10-16-0619-PDT.tgz"),
        ("  ", "unifi-support-2026-10-16-0619-PDT.tgz"),
        (None, "unifi-support-2026-10-16-0619-PDT.tgz"),
    ],
)
def test_the_file_name_says_which_device_and_when(tmp_path, label, expected) -> None:
    assert reserve_support_file(tmp_path, label, WHEN) == tmp_path / expected


def test_an_existing_file_is_never_overwritten(tmp_path) -> None:
    first = reserve_support_file(tmp_path, "AP", WHEN)
    first.write_bytes(b"earlier")
    second = reserve_support_file(tmp_path, "AP", WHEN)
    second.write_bytes(b"later")

    assert second == tmp_path / "AP-support-2026-10-16-0619-PDT-2.tgz"
    assert reserve_support_file(tmp_path, "AP", WHEN) == tmp_path / "AP-support-2026-10-16-0619-PDT-3.tgz"
    assert first.read_bytes() == b"earlier"


def test_reserving_creates_the_file_so_a_concurrent_caller_gets_another_name(tmp_path) -> None:
    first = reserve_support_file(tmp_path, "AP", WHEN)
    assert first.exists() and first.stat().st_size == 0
    assert reserve_support_file(tmp_path, "AP", WHEN) != first


def test_the_zone_is_reduced_to_safe_characters(tmp_path) -> None:
    offset = dt.datetime(2026, 10, 16, 6, 19, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert reserve_support_file(tmp_path, "AP", offset).name == "AP-support-2026-10-16-0619-UTC-05-30.tgz"
    spelled = dt.datetime(2026, 10, 16, 6, 19, tzinfo=dt.timezone(dt.timedelta(hours=-7), "Pacific Daylight Time"))
    assert reserve_support_file(tmp_path, "AP", spelled).name == (
        "AP-support-2026-10-16-0619-Pacific-Daylight-Time.tgz"
    )


def test_a_time_without_a_zone_says_local(tmp_path) -> None:
    naive = dt.datetime(2026, 10, 16, 6, 19)
    assert reserve_support_file(tmp_path, "AP", naive).name == "AP-support-2026-10-16-0619-local.tgz"


# ---- what the controller hands back ----


@pytest.mark.parametrize("body", [b"", b"<html>login</html>"])
async def test_a_download_that_is_not_a_support_archive_is_discarded(tmp_path, body) -> None:
    connection = Connection(statuses=["READY_TO_DOWNLOAD"], body=body)
    dest = tmp_path / "ap.tgz"

    with pytest.raises(RuntimeError, match="not a support file"):
        await manager(connection, Clock()).generate_device_support_file(MAC, dest)
    assert not dest.exists()


async def test_a_generation_the_controller_drops_is_reported_not_waited_out(tmp_path) -> None:
    connection = Connection(statuses=["READY_TO_GENERATE", "GENERATING", "READY_TO_GENERATE"])
    clock = Clock()

    with pytest.raises(RuntimeError, match="not generating"):
        await manager(connection, clock, timeout=600).generate_device_support_file(MAC, tmp_path / "ap.tgz")
    assert clock.now < 600


@pytest.mark.parametrize("rows", [[], {}, ["READY_TO_DOWNLOAD"]])
async def test_a_status_reply_of_the_wrong_shape_is_an_error(tmp_path, rows) -> None:
    connection = Connection(statuses=[rows])

    with pytest.raises(RuntimeError, match="Unexpected support-file status"):
        await manager(connection, Clock()).generate_device_support_file(MAC, tmp_path / "ap.tgz")


async def test_the_console_file_needs_a_unifi_os_console(tmp_path) -> None:
    connection = Connection()
    connection.is_unifi_os = False

    with pytest.raises(RuntimeError, match="UniFi OS"):
        await manager(connection, Clock()).generate_console_support_file(tmp_path / "console.tgz")
    assert connection.calls == []


async def test_a_controller_without_session_credentials_is_refused_first(tmp_path) -> None:
    from unifi_core.exceptions import UniFiAuthError

    connection = Connection(statuses=["READY_TO_GENERATE"])
    connection.authentication_status = SimpleNamespace(session_configured=False, api_key_available=None)
    support = manager(connection, Clock())

    with pytest.raises(UniFiAuthError, match="session authentication"):
        support.require_session_auth()
    with pytest.raises(UniFiAuthError):
        await support.generate_device_support_file(MAC, tmp_path / "ap.tgz")
    with pytest.raises(UniFiAuthError):
        await support.generate_console_support_file(tmp_path / "console.tgz")
    assert connection.calls == []


async def test_two_console_requests_run_one_after_the_other(tmp_path) -> None:
    connection = Connection(downloads=[423, 200, 423, 200])
    support = manager(connection, Clock())

    await asyncio.gather(
        support.generate_console_support_file(tmp_path / "one.tgz"),
        support.generate_console_support_file(tmp_path / "two.tgz"),
    )

    kinds = [kind for kind, _ in connection.calls]
    # the second generate starts only after the first file is down
    assert kinds == ["post", "download", "download", "post", "download", "download"]


@pytest.mark.parametrize("body", [b"", b"<html>login</html>"])
async def test_a_console_download_that_is_not_a_support_archive_is_discarded(tmp_path, body) -> None:
    connection = Connection(body=body)
    dest = tmp_path / "console.tgz"

    with pytest.raises(RuntimeError, match="not a support file"):
        await manager(connection, Clock()).generate_console_support_file(dest)
    assert not dest.exists()


async def test_the_console_check_waits_for_the_connection_to_know_the_controller_type(tmp_path) -> None:
    connection = Connection()
    connection.is_unifi_os = False  # unknown until connected

    async def connect() -> bool:
        connection.is_unifi_os = True
        return True

    connection.ensure_connected = connect

    await manager(connection, Clock()).generate_console_support_file(tmp_path / "console.tgz")


async def test_two_requests_for_the_same_device_run_one_after_the_other(tmp_path) -> None:
    connection = Connection(
        statuses=["READY_TO_GENERATE", "READY_TO_DOWNLOAD", "READY_TO_GENERATE", "READY_TO_DOWNLOAD"]
    )
    support = manager(connection, Clock())

    await asyncio.gather(
        support.generate_device_support_file(MAC, tmp_path / "one.tgz"),
        support.generate_device_support_file(MAC, tmp_path / "two.tgz"),
    )

    kinds = [kind for kind, _ in connection.calls]
    assert kinds == ["get", "post", "get", "download", "get", "post", "get", "download"]


async def test_a_device_and_the_console_do_not_wait_for_each_other(tmp_path) -> None:
    connection = Connection(statuses=["READY_TO_GENERATE", "GENERATING", "READY_TO_DOWNLOAD"], downloads=[423, 200])
    support = manager(connection, Clock())

    await asyncio.gather(
        support.generate_device_support_file(MAC, tmp_path / "ap.tgz"),
        support.generate_console_support_file(tmp_path / "console.tgz"),
    )

    paths = [path for _, path in connection.calls]
    # the console starts before the device's file is down
    assert paths.index("/api/support/file/generate") < paths.index(DEVICE_DOWNLOAD)


async def test_waiting_right_up_to_the_deadline_is_allowed(tmp_path) -> None:
    connection = Connection(statuses=["READY_TO_GENERATE", "GENERATING", "READY_TO_DOWNLOAD"])
    clock = Clock()

    await manager(connection, clock, timeout=10).generate_device_support_file(MAC, tmp_path / "ap.tgz")
    assert clock.now == 10


async def test_a_session_that_fell_back_to_an_api_key_is_refused(tmp_path) -> None:
    from unifi_core.exceptions import UniFiAuthError

    connection = Connection(statuses=["READY_TO_GENERATE"])
    connection.authentication_status = SimpleNamespace(session_configured=True, api_key_available=True)

    with pytest.raises(UniFiAuthError, match="session authentication"):
        await manager(connection, Clock()).generate_device_support_file(MAC, tmp_path / "ap.tgz")
    assert connection.calls == []


def test_a_cut_name_does_not_end_in_a_separator(tmp_path) -> None:
    assert reserve_support_file(tmp_path, "a" * 63 + " b", WHEN).name == "a" * 63 + "-support-2026-10-16-0619-PDT.tgz"


def test_a_reserved_file_and_a_new_folder_are_private(tmp_path) -> None:
    path = reserve_support_file(tmp_path / "new", "AP", WHEN)

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


async def test_the_generate_request_carries_an_empty_json_object(tmp_path) -> None:
    """The controller ignores a body-less POST (seen live); the web UI sends {}."""
    connection = Connection(statuses=["READY_TO_GENERATE", "READY_TO_DOWNLOAD"])

    await manager(connection, Clock()).generate_device_support_file(MAC, tmp_path / "ap.tgz")

    post = connection.calls.index(("post", f"/support/{MAC}/generation"))
    assert connection.bodies[post] == {}
