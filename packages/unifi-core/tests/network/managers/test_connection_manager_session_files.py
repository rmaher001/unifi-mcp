"""Session-authenticated file transfer over real HTTPS through aiounifi's login.

The controller is a local TLS server. ConnectionManager.initialize logs in for
real, so the session cookie and CSRF header these requests must carry are the
ones the console issued, not values the test planted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiounifi.models.api import ApiRequestV2
from unifi_core.exceptions import UniFiAuthError
from unifi_core.network.managers.connection_manager import ConnectionManager, SessionDownload

from tests.network.local_tls import tls_context

PAYLOAD = b"\x1f\x8b" + bytes(range(256)) * 4096  # ~1 MiB, gzip magic first


class Console:
    """A UniFi OS console with a support-file style download and a JSON POST."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, str | None, str | None]] = []
        self.logins = 0
        self.expire_session_once = False
        self.download_status = 200
        self.drop_mid_stream = False
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
        if request.cookies.get("TOKEN") != f"fixture-session-{self.logins}":
            return web.json_response({"error": "unauthorized"}, status=401)
        if self.expire_session_once:
            self.expire_session_once = False
            return web.json_response({"error": "unauthorized"}, status=401)
        if path == "/api/support/file/generate" and request.method == "POST":
            self.posted.append(await request.json())
            return web.json_response({})
        if path == "/api/support/file/download":
            if self.download_status != 200:
                return web.json_response({"code": "SUPPORT_FILE_NOT_READY"}, status=self.download_status)
            response = web.StreamResponse(headers={"filename": "support-fixture.tgz"})
            response.content_type = "application/x-gzip"
            await response.prepare(request)
            for start in range(0, len(PAYLOAD), 65536):
                await response.write(PAYLOAD[start : start + 65536])
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

    assert result == SessionDownload(status=200, size_bytes=len(PAYLOAD), filename="support-fixture.tgz")
    assert dest.read_bytes() == PAYLOAD
    method, _, csrf, cookie = console.seen[-1]
    assert (method, csrf, cookie) == ("GET", "fixture-csrf-1", "fixture-session-1")
    assert list(dest.parent.iterdir()) == [dest]  # no partial file left beside it


async def test_a_refused_download_writes_nothing_and_reports_the_status(connected, tmp_path) -> None:
    connection, console = connected
    console.download_status = 423
    dest = tmp_path / "console.tgz"

    result = await connection.session_download("/api/support/file/download", dest)

    assert result == SessionDownload(status=423, size_bytes=0, filename=None)
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

    with pytest.raises(Exception):  # noqa: B017 - whichever transport error aiohttp raises
        await connection.session_download("/api/support/file/download", dest)
    assert list(downloads.iterdir()) == []


@pytest.mark.parametrize("path", ["api/support/file/download", "/api/../secret", "https://elsewhere/x", "/a?b=c"])
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
