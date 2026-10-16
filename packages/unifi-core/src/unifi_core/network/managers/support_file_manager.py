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
import datetime as dt
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from aiounifi.models.api import ApiRequestV2

from unifi_core.exceptions import UniFiAuthError
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
GZIP_MAGIC = b"\x1f\x8b"
MAX_LABEL_LENGTH = 64


_UNSAFE_NAME_RUN = re.compile(r"[^\w.]+")


def _safe_part(text: str | None) -> str:
    return _UNSAFE_NAME_RUN.sub("-", text or "").strip("-.")


def reserve_support_file(directory: Path, label: str | None, when: dt.datetime) -> Path:
    """Create and return an empty ``<label>-support-YYYY-MM-DD-HHMM-<TZ>.tgz`` in ``directory``.

    ``label`` is the device or console name, reduced to word characters and
    ``.`` joined by single hyphens and cut to 64 characters, so it stays
    inside ``directory``. The time is shown in ``when``'s own zone. The file
    is created exclusively, so neither an earlier file nor a concurrent caller
    is ever overwritten: a ``-2``, ``-3``, ... suffix is added instead.
    """
    stem = _safe_part(label)[:MAX_LABEL_LENGTH].strip("-.") or "unifi"
    zone = _safe_part(when.strftime("%Z")) or "local"
    base = f"{stem}-support-{when.strftime('%Y-%m-%d-%H%M')}-{zone}"
    directory.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        candidate = directory / (f"{base}.tgz" if suffix == 1 else f"{base}-{suffix}.tgz")
        try:
            candidate.open("x").close()
        except FileExistsError:
            suffix += 1
            continue
        return candidate


@dataclass(frozen=True)
class SupportFile:
    """A support file saved to disk."""

    path: Path
    size_bytes: int


class SupportFileManager:
    """Generates and downloads device and console support files, one at a time per device or console."""

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
        self._locks: dict[str, asyncio.Lock] = {}

    def require_session_auth(self) -> None:
        """Raise unless username/password session credentials are configured; API keys cannot do this."""
        if not self._connection.authentication_status.session_configured:
            raise UniFiAuthError(
                "Support files require Network session authentication. "
                "Configure UNIFI_NETWORK_USERNAME and UNIFI_NETWORK_PASSWORD."
            )

    async def generate_device_support_file(self, mac: str, dest: Path) -> SupportFile:
        """Generate a device's support file (unless one is already under way or ready) and save it to ``dest``."""
        device = canonical_mac(mac)
        if device is None:
            raise ValueError("A device MAC address is required, e.g. aa:bb:cc:dd:ee:ff.")
        self.require_session_auth()
        async with self._lock(device):
            deadline = self._clock() + self._timeout
            status = await self._device_support_status(device)
            if status == READY_TO_GENERATE:
                await self._connection.request(ApiRequestV2(method="post", path=f"/support/{device}/generation"))
                status = GENERATING
            while status != READY_TO_DOWNLOAD:
                await self._wait(deadline)
                status = await self._device_support_status(device)
                if status == READY_TO_GENERATE:
                    raise RuntimeError(
                        "The controller stopped generating the device support file; the device may be offline."
                    )

            path = self._connection.network_path(ApiRequestV2(method="get", path=f"/support/{device}/download"))
            result = await self._connection.session_download(path, dest)
            if result.status == DEVICE_ALREADY_TAKEN:
                raise RuntimeError(
                    "The device support file is no longer available; a file can be downloaded "
                    "only once, and another download took it. Generate it again."
                )
            if result.status != 200:
                raise RuntimeError(f"The device support file download failed (HTTP {result.status}).")
            return await self._checked(dest, result.size_bytes)

    async def generate_console_support_file(self, dest: Path) -> SupportFile:
        """Generate the UniFi OS console's support file and save it to ``dest``."""
        self.require_session_auth()
        async with self._lock("console"):
            await self._connection.ensure_connected()
            if not self._connection.is_unifi_os:
                raise RuntimeError("The console support file is available only on a UniFi OS console.")
            deadline = self._clock() + self._timeout
            status = await self._connection.session_post(CONSOLE_GENERATE_PATH, {"recreate": True})
            if not 200 <= status < 300:
                raise RuntimeError(f"The console refused to generate a support file (HTTP {status}).")
            while True:
                result = await self._connection.session_download(CONSOLE_DOWNLOAD_PATH, dest)
                if result.status == 200:
                    return await self._checked(dest, result.size_bytes)
                if result.status != CONSOLE_NOT_READY:
                    raise RuntimeError(f"The console support file download failed (HTTP {result.status}).")
                await self._wait(deadline)

    async def _device_support_status(self, device: str) -> str:
        rows = await self._connection.request(ApiRequestV2(method="get", path=f"/support/{device}"))
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        status = row.get("status")
        if status not in (READY_TO_GENERATE, GENERATING, READY_TO_DOWNLOAD):
            raise RuntimeError(f"Unexpected support-file status for device: {status!r}")
        return status

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def _checked(self, dest: Path, size: int) -> SupportFile:
        """Keep the download only if it is a gzip archive, as every support file is."""

        def head() -> bytes:
            with dest.open("rb") as handle:
                return handle.read(len(GZIP_MAGIC))

        if size == 0 or await asyncio.to_thread(head) != GZIP_MAGIC:
            await asyncio.to_thread(dest.unlink, missing_ok=True)
            raise RuntimeError("The controller returned something that is not a support file archive.")
        return SupportFile(path=dest, size_bytes=size)

    async def _wait(self, deadline: float) -> None:
        """Sleep one poll interval, or raise if that would pass the deadline."""
        if self._clock() + self._poll_interval > deadline:
            raise TimeoutError(f"The support file was not ready within {self._timeout:g} seconds.")
        await self._sleep(self._poll_interval)
