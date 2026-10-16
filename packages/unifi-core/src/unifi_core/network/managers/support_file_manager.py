"""Controller support files: have the controller build one, wait for it, save it to disk.

These are the ``.tgz`` files the web UI's *Generate / Download Support File*
buttons produce, for a device (AP, switch, ...) or for the UniFi OS console
itself. They hold logs and configuration, are megabytes in size, and are
written to a local path; their contents are never returned.

Device files live on the Network v2 API:

* ``GET  /support/{mac}`` reports ``READY_TO_GENERATE``, ``GENERATING`` or ``READY_TO_DOWNLOAD``
* ``POST /support/{mac}/generation`` starts one
* ``GET  /support/{mac}/download`` returns it, once: afterwards the status is
  ``READY_TO_GENERATE`` again and the download answers 400

The console file is a UniFi OS endpoint outside ``/proxy/network``:

* ``POST /api/support/file/generate`` with ``{"recreate": true}`` starts one
* ``GET  /api/support/file/download`` answers 423 until it is ready, then
  returns it, also once
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from aiounifi.models.api import ApiRequestV2

from unifi_core.mac import canonical_mac

logger = logging.getLogger("unifi-network-mcp")

READY_TO_GENERATE = "READY_TO_GENERATE"
GENERATING = "GENERATING"
READY_TO_DOWNLOAD = "READY_TO_DOWNLOAD"
# The download answers 400 api.err.NoDeviceSupportFileExist once the file has been taken.
DEVICE_ALREADY_TAKEN = 400

CONSOLE_GENERATE_PATH = "/api/support/file/generate"
CONSOLE_DOWNLOAD_PATH = "/api/support/file/download"
CONSOLE_NOT_READY = 423


@dataclass(frozen=True)
class SupportFile:
    """A support file saved to disk."""

    path: Path
    size_bytes: int


class SupportFileManager:
    """Generates and downloads device and console support files."""

    def __init__(
        self,
        connection_manager,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ):
        self._connection = connection_manager
        self._sleep = sleep
        self._clock = clock
        self._poll_interval = poll_interval
        self._timeout = timeout

    async def device_support_status(self, mac: str) -> str:
        """The controller's support-file status for one device."""
        rows = await self._connection.request(ApiRequestV2(method="get", path=f"/support/{mac}"))
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        status = row.get("status")
        if status not in (READY_TO_GENERATE, GENERATING, READY_TO_DOWNLOAD):
            raise RuntimeError(f"Unexpected support-file status for device: {status!r}")
        return status

    async def generate_device_support_file(self, mac: str, dest: Path) -> SupportFile:
        """Generate a device's support file (unless one is already under way or ready) and save it to ``dest``."""
        device = canonical_mac(mac)
        if device is None:
            raise ValueError("A device MAC address is required, e.g. aa:bb:cc:dd:ee:ff.")

        deadline = self._clock() + self._timeout
        status = await self.device_support_status(device)
        if status == READY_TO_GENERATE:
            await self._connection.request(ApiRequestV2(method="post", path=f"/support/{device}/generation"))
            status = GENERATING
        while status != READY_TO_DOWNLOAD:
            await self._wait(deadline)
            status = await self.device_support_status(device)

        path = self._connection.network_path(ApiRequestV2(method="get", path=f"/support/{device}/download"))
        result = await self._connection.session_download(path, dest)
        if result.status == DEVICE_ALREADY_TAKEN:
            raise RuntimeError(
                "The device support file is no longer available; a file can be downloaded "
                "only once, and another download took it. Generate it again."
            )
        if result.status != 200:
            raise RuntimeError(f"The device support file download failed (HTTP {result.status}).")
        return SupportFile(path=dest, size_bytes=result.size_bytes)

    async def generate_console_support_file(self, dest: Path) -> SupportFile:
        """Generate the UniFi OS console's support file and save it to ``dest``."""
        deadline = self._clock() + self._timeout
        status = await self._connection.session_post(CONSOLE_GENERATE_PATH, {"recreate": True})
        if not 200 <= status < 300:
            raise RuntimeError(f"The console refused to generate a support file (HTTP {status}).")
        while True:
            result = await self._connection.session_download(CONSOLE_DOWNLOAD_PATH, dest)
            if result.status == 200:
                return SupportFile(path=dest, size_bytes=result.size_bytes)
            if result.status != CONSOLE_NOT_READY:
                raise RuntimeError(f"The console support file download failed (HTTP {result.status}).")
            await self._wait(deadline)

    async def _wait(self, deadline: float) -> None:
        """Sleep one poll interval, or raise if that would pass the deadline."""
        if self._clock() + self._poll_interval > deadline:
            raise TimeoutError(f"The support file was not ready within {self._timeout:g} seconds.")
        await self._sleep(self._poll_interval)
