"""Session-authenticated file transfer over real HTTPS through aiounifi's login.

The controller is a local TLS server. ConnectionManager.initialize logs in for
real, so the session cookie and CSRF header these requests must carry are the
ones the console issued, not values the test planted.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiounifi.errors import RequestError
from aiounifi.models.api import ApiRequestV2
from unifi_core.exceptions import UniFiAuthError
from unifi_core.network.managers.connection_manager import ConnectionManager, SessionDownload

from tests.network.local_tls import tls_context

PAYLOAD = b"\x1f\x8b" + bytes(range(256)) * 4096  # ~1 MiB, gzip magic first


class Console:
    """A UniFi OS console with a support-file style download and a JSON POST."""

    def expire_current_session(self) -> None:
        self.expired_session = f"fixture-session-{self.logins}"

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, str | None, str | None]] = []
        self.logins = 0
        self.expire_session_once = False
        self.download_status = 200
        self.drop_mid_stream = False
        self.always_unauthorized = False
        self.redirect_download = False
        self.stall_seconds = 0.0
        self.post_status = 200
        self.expired_session: str | None = None
        self.refusals = 0
        self.posted: list[object] = []
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.app = app

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        self.seen.append((request.method, path, request.headers.get("x-csrf-token"), request.cookies.get("TOKEN")))
        if path == "/":
            return web.Response(text="UniFi OS")
        if path == "/api/auth/login":
            self.logins += 1
            response = web.json_response({"unique_id": "fixture-user"})
            response.set_cookie("TOKEN", f"fixture-session-{self.logins}")
            response.headers["x-csrf-token"] = f"fixture-csrf-{self.logins}"
            return response
        if path.endswith("/self/sites"):
            return web.json_response({"meta": {"rc": "ok"}, "data": [{"name": "default", "desc": "Default"}]})
        if self.expired_session is not None and request.cookies.get("TOKEN") == self.expired_session:
            # Each later request is refused later, after the first has logged in again.
            self.refusals += 1
            await asyncio.sleep(0.2 * self.refusals)
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.cookies.get("TOKEN") != f"fixture-session-{self.logins}":
            return web.json_response({"error": "unauthorized"}, status=401)
        if self.expire_session_once or self.always_unauthorized:
            self.expire_session_once = False
            # Large enough that aiohttp cannot free the connection by itself having read it all.
            return web.Response(status=401, body=b"x" * (4 << 20))
        if self.redirect_download and path == "/api/support/file/download":
            raise web.HTTPFound("https://elsewhere.invalid/steal")
        if path == "/api/support/file/generate" and request.method == "POST":
            self.posted.append(await request.json())
            return web.json_response({}, status=self.post_status)
        if path == "/api/support/file/download":
            if self.download_status != 200:
                return web.json_response({"code": "SUPPORT_FILE_NOT_READY"}, status=self.download_status)
            response = web.StreamResponse(headers={"filename": "support-fixture.tgz"})
            response.content_type = "application/x-gzip"
            await response.prepare(request)
            for start in range(0, len(PAYLOAD), 65536):
                await response.write(PAYLOAD[start : start + 65536])
                if self.stall_seconds and start >= len(PAYLOAD) // 2:
                    await asyncio.sleep(self.stall_seconds)
                if self.drop_mid_stream and start >= len(PAYLOAD) // 2:
                    request.transport.close()
                    return response
            await response.write_eof()
            return response
        return web.Response(status=404)


@pytest.fixture
async def connected(tmp_path):
    console = Console()
    server = TestServer(console.app)
    await server.start_server(ssl=tls_context(tmp_path))
    connection = ConnectionManager("127.0.0.1", "fixture-user", "fixture-password", port=server.port, retry_delay=0)
    assert await connection.initialize()
    try:
        yield connection, console
    finally:
        await connection.cleanup()
        await server.close()


async def test_a_download_streams_to_disk_with_the_session_cookie_and_csrf_header(connected, tmp_path) -> None:
    connection, console = connected
    dest = tmp_path / "out" / "console.tgz"

    result = await connection.session_download("/api/support/file/download", dest)

    assert result == SessionDownload(status=200, size_bytes=len(PAYLOAD))
    assert dest.read_bytes() == PAYLOAD
    method, _, csrf, cookie = console.seen[-1]
    assert (method, csrf, cookie) == ("GET", "fixture-csrf-1", "fixture-session-1")
    assert list(dest.parent.iterdir()) == [dest]  # no partial file left beside it


async def test_a_refused_download_writes_nothing_and_reports_the_status(connected, tmp_path) -> None:
    connection, console = connected
    console.download_status = 423
    dest = tmp_path / "console.tgz"

    result = await connection.session_download("/api/support/file/download", dest)

    assert result == SessionDownload(status=423, size_bytes=0)
    assert not dest.exists()
    assert not list(tmp_path.glob("*.part"))


async def test_an_expired_session_logs_in_again_once_and_retries(connected, tmp_path) -> None:
    connection, console = connected
    console.expire_session_once = True
    dest = tmp_path / "console.tgz"

    result = await connection.session_download("/api/support/file/download", dest)

    assert result.status == 200
    assert console.logins == 2
    assert dest.read_bytes() == PAYLOAD


async def test_a_post_sends_json_with_the_csrf_header(connected) -> None:
    connection, console = connected

    status = await connection.session_post("/api/support/file/generate", {"recreate": True})

    assert status == 200
    assert console.posted == [{"recreate": True}]
    method, path, csrf, cookie = console.seen[-1]
    assert (method, path, csrf, cookie) == ("POST", "/api/support/file/generate", "fixture-csrf-1", "fixture-session-1")


async def test_a_post_after_an_expired_session_logs_in_again_once(connected) -> None:
    connection, console = connected
    console.expire_session_once = True

    assert await connection.session_post("/api/support/file/generate", {"recreate": True}) == 200
    assert console.logins == 2
    assert console.posted == [{"recreate": True}]


async def test_a_connection_dropped_mid_stream_leaves_no_file(connected, tmp_path) -> None:
    connection, console = connected
    console.drop_mid_stream = True
    downloads = tmp_path / "downloads"
    dest = downloads / "console.tgz"

    with pytest.raises(RequestError) as raised:
        await connection.session_download("/api/support/file/download", dest)
    assert list(downloads.iterdir()) == []
    # the safe form: classified as a transport failure, no URL or address in it
    assert str(raised.value).startswith("Error requesting data from ")
    assert "127.0.0.1" not in str(raised.value) and "/api/" not in str(raised.value)


@pytest.mark.parametrize(
    "path", ["api/support/file/download", "/api/../secret", "https://elsewhere/x", "/a?b=c", "//elsewhere/x", "/a#b"]
)
async def test_paths_must_be_plain_absolute_paths_on_the_controller(connected, tmp_path, path) -> None:
    connection, console = connected
    before = len(console.seen)

    with pytest.raises(ValueError):
        await connection.session_download(path, tmp_path / "x.tgz")
    with pytest.raises(ValueError):
        await connection.session_post(path, {})
    assert len(console.seen) == before


async def test_api_key_mode_is_refused_before_anything_is_sent(tmp_path) -> None:
    connection = ConnectionManager("controller.invalid", "", "")
    connection._key_mode = True
    connection.ensure_connected = AsyncMock(return_value=True)

    with pytest.raises(UniFiAuthError, match="session authentication"):
        await connection.session_download("/api/support/file/download", tmp_path / "x.tgz")
    with pytest.raises(UniFiAuthError, match="session authentication"):
        await connection.session_post("/api/support/file/generate", {})
    connection.ensure_connected.assert_not_awaited()


@pytest.mark.parametrize(
    ("override", "detected", "expected"),
    [
        (True, False, "/proxy/network/v2/api/site/branch/support/aa:bb:cc:dd:ee:ff"),
        (False, True, "/v2/api/site/branch/support/aa:bb:cc:dd:ee:ff"),
        (None, True, "/proxy/network/v2/api/site/branch/support/aa:bb:cc:dd:ee:ff"),
        (None, False, "/v2/api/site/branch/support/aa:bb:cc:dd:ee:ff"),
    ],
)
def test_network_path_is_the_path_request_would_send_to(override, detected, expected) -> None:
    connection = ConnectionManager("controller.invalid", "fixture-user", "fixture-password", site="branch")
    connection._unifi_os_override = override
    connection.controller = MagicMock()
    connection.controller.connectivity.is_unifi_os = detected

    assert connection.network_path(ApiRequestV2(method="get", path="/support/aa:bb:cc:dd:ee:ff")) == expected


async def test_a_redirect_is_never_followed_with_the_session(connected, tmp_path) -> None:
    connection, console = connected
    console.redirect_download = True

    result = await connection.session_download("/api/support/file/download", tmp_path / "x.tgz")

    assert result.status == 302
    assert not (tmp_path / "x.tgz").exists()


async def test_a_post_reports_the_controllers_status(connected) -> None:
    connection, console = connected
    console.post_status = 403

    assert await connection.session_post("/api/support/file/generate", {"recreate": True}) == 403


async def test_a_session_refused_again_after_logging_in_stops_further_logins(connected, tmp_path) -> None:
    connection, console = connected
    console.always_unauthorized = True

    with pytest.raises(UniFiAuthError, match="after logging in again"):
        await connection.session_download("/api/support/file/download", tmp_path / "x.tgz")
    assert console.logins == 2
    # like request(): the refused session is terminal, so a later call does not log in again
    with pytest.raises(Exception):  # noqa: B017 - not connected, whichever form ensure_connected reports
        await connection.session_post("/api/support/file/generate", {})
    assert console.logins == 2


async def test_a_failed_login_after_expiry_is_an_auth_error(connected, tmp_path) -> None:
    connection, console = connected
    console.expire_session_once = True
    connection._reauthenticate = AsyncMock(return_value=False)

    with pytest.raises(UniFiAuthError, match="logging in again failed"):
        await connection.session_download("/api/support/file/download", tmp_path / "x.tgz")


async def test_no_connection_is_reported_before_anything_is_sent(tmp_path) -> None:
    connection = ConnectionManager("controller.invalid", "fixture-user", "fixture-password")
    connection.ensure_connected = AsyncMock(return_value=False)

    with pytest.raises(ConnectionError):
        await connection.session_download("/api/support/file/download", tmp_path / "x.tgz")


async def test_a_slow_steady_download_is_not_cut_off_but_a_stalled_one_is(connected, tmp_path) -> None:
    connection, console = connected
    console.stall_seconds = 0.4

    result = await connection.session_download("/api/support/file/download", tmp_path / "a.tgz", read_timeout=2)
    assert result.size_bytes == len(PAYLOAD)

    with pytest.raises(RequestError):
        await connection.session_download("/api/support/file/download", tmp_path / "b.tgz", read_timeout=0.1)
    assert not (tmp_path / "b.tgz").exists()


def test_is_unifi_os_follows_the_override_then_detection() -> None:
    connection = ConnectionManager("controller.invalid", "fixture-user", "fixture-password")
    assert connection.is_unifi_os is False  # not connected, nothing detected
    connection.controller = MagicMock()
    connection.controller.connectivity.is_unifi_os = True
    assert connection.is_unifi_os is True
    connection._unifi_os_override = False
    assert connection.is_unifi_os is False


async def test_a_controller_that_cannot_be_reached_is_reported_without_its_address(connected, tmp_path) -> None:
    connection, _ = connected

    async def refused(*args, **kwargs):
        raise aiohttp.ClientOSError(61, "Cannot connect to host 127.0.0.1:8443 ssl:default [Connect call failed]")

    connection.controller.connectivity.config.session.request = refused

    for call in (
        connection.session_download("/api/support/file/download", tmp_path / "x.tgz"),
        connection.session_post("/api/support/file/generate", {}),
    ):
        with pytest.raises(RequestError) as raised:
            await call
        assert str(raised.value) == "Error requesting data from the controller: ClientOSError"


async def test_a_terminal_refusal_drops_the_session(connected, tmp_path) -> None:
    connection, console = connected
    console.always_unauthorized = True

    with pytest.raises(UniFiAuthError):
        await connection.session_download("/api/support/file/download", tmp_path / "x.tgz")
    assert connection.controller is None and connection._aiohttp_session is None


async def test_every_response_is_released_including_refused_ones(connected, tmp_path) -> None:
    connection, console = connected
    connector = connection._aiohttp_session.connector
    console.expire_session_once = True
    await connection.session_download("/api/support/file/download", tmp_path / "a.tgz")
    console.redirect_download = True
    await connection.session_download("/api/support/file/download", tmp_path / "b.tgz")
    console.redirect_download = False
    console.download_status = 423
    await connection.session_download("/api/support/file/download", tmp_path / "c.tgz")

    assert not connector._acquired  # no connection left checked out


async def test_concurrent_requests_on_an_expired_session_log_in_again_only_once(connected, tmp_path) -> None:
    connection, console = connected
    console.expire_current_session()

    await asyncio.gather(
        connection.session_download("/api/support/file/download", tmp_path / "a.tgz"),
        connection.session_download("/api/support/file/download", tmp_path / "b.tgz"),
    )
    assert console.logins == 2


async def test_a_download_is_bounded_by_silence_not_by_its_length(connected, tmp_path) -> None:
    connection, _ = connected
    session = connection.controller.connectivity.config.session
    seen = {}
    real = session.request

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    session.request = spy
    await connection.session_download("/api/support/file/download", tmp_path / "x.tgz", read_timeout=45)

    timeout = seen["timeout"]
    assert (timeout.total, timeout.sock_connect, timeout.sock_read) == (None, 30, 45)


async def test_a_stale_partial_file_is_left_alone(connected, tmp_path) -> None:
    connection, _ = connected
    stale = tmp_path / "x.tgz.part"
    stale.write_bytes(b"someone else's")

    await connection.session_download("/api/support/file/download", tmp_path / "x.tgz")

    assert stale.read_bytes() == b"someone else's"
    assert (tmp_path / "x.tgz").read_bytes() == PAYLOAD


async def test_a_cancelled_download_leaves_no_file(connected, tmp_path) -> None:
    connection, console = connected
    console.stall_seconds = 5
    downloads = tmp_path / "downloads"
    task = asyncio.create_task(connection.session_download("/api/support/file/download", downloads / "x.tgz"))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if downloads.exists() and any(downloads.iterdir()):
            break
    assert any(downloads.iterdir()), "the partial file never appeared"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(downloads.iterdir()) == []


async def test_a_downloaded_file_is_readable_only_by_its_owner(connected, tmp_path) -> None:
    connection, _ = connected
    dest = tmp_path / "x.tgz"

    await connection.session_download("/api/support/file/download", dest)

    assert dest.stat().st_mode & 0o777 == 0o600
