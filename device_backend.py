"""macOS device access. All subprocess/CLI calls live in this module.

API/command shapes inspected against pymobiledevice3 11.27.2. Runtime help and
API checks fail explicitly if a different installed release changes them.
"""

from __future__ import annotations

import asyncio
import getpass
import inspect
import json
import math
import os
import platform
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import traceback
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

ISSUES_URL = "https://github.com/doronz88/pymobiledevice3/issues"
MIN_MACOS = (26, 4)  # Updated from 27 at the user's request.
DEVICE_TIMEOUT = 20
TUNNEL_TIMEOUT = 60


class DeviceError(Exception):
    """Safe public message plus optional technical detail for the later UI."""

    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def friendly_error(exc: Exception, operation: str = "device access") -> DeviceError:
    if isinstance(exc, DeviceError):
        return exc
    detail = "".join(traceback.format_exception(exc))
    text = f"{type(exc).__name__} {exc}".lower()
    rules = (
        (("passwordrequired", "passcoderequired", "passwordprotected", "devicelocked", "device is locked"),
         "locked", "The iPhone is locked. Unlock it, keep its screen awake, and try again."),
        (("notpaired", "pairingdialogresponsepending", "userdeniedpairing", "invalidhostid", "invalidpairrecord"),
         "not_trusted", "This Mac is not trusted. Unlock the iPhone, select it in Finder, and accept Trust on both devices."),
        (("developermodeisnotenabled", "developer mode is not enabled", "developer mode is disabled"),
         "developer_mode_off", "Developer Mode is off. Enable Settings > Privacy & Security > Developer Mode, restart, and confirm."),
        (("notmounted", "developerdiskimagenotfound", "image is not mounted"),
         "image_not_mounted", "The developer image is not mounted. Run python app.py mount, then try again."),
        (("nodeviceconnected", "devicenotfound", "connectionfailedtousbmuxd"),
         "no_device", "No USB iPhone is available. Connect it with a data cable, unlock it, and check Finder."),
        (("tunneldconnection", "rsdrequired", "tunnel not running"),
         "tunnel_not_running", "The device tunnel is unavailable. Retry and approve the macOS administrator dialog."),
        (("invalidservice", "unsupportedservice", "unrecognizedselector", "devicefeaturenotsupported",
          "unsupported service", "unknown service", "does not support"),
         "unsupported_service", f"The installed library or iOS does not support this service. Try pip install -U pymobiledevice3 and check {ISSUES_URL}."),
    )
    for needles, code, message in rules:
        if any(needle in text for needle in needles):
            return DeviceError(code, message, detail)
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return DeviceError("dependency", "A required library API is unavailable. Run python -m pip install -U -r requirements.txt.", detail)
    if isinstance(exc, TimeoutError):
        return DeviceError("timeout", f"{operation.capitalize()} timed out. Check that the iPhone is connected and unlocked, then retry.", detail)
    return DeviceError("unexpected", f"Could not complete {operation}. Check the USB connection and retry.", detail)


def validate_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    try:
        lat, lng = float(latitude), float(longitude)
    except (TypeError, ValueError) as exc:
        raise DeviceError("coordinates", "Latitude and longitude must be numbers.") from exc
    if not math.isfinite(lat) or not -90 <= lat <= 90:
        raise DeviceError("coordinates", "Latitude must be a finite number from -90 to 90.")
    if not math.isfinite(lng) or not -180 <= lng <= 180:
        raise DeviceError("coordinates", "Longitude must be a finite number from -180 to 180.")
    return lat, lng


def installed_version() -> str:
    try:
        return version("pymobiledevice3")
    except PackageNotFoundError:
        return "not installed"


def require_supported_host() -> str:
    if sys.version_info < (3, 12):
        raise DeviceError("platform", "Use Python 3.12 or newer.")
    if platform.system() != "Darwin":
        raise DeviceError("platform", "Device operations require an Apple Silicon Mac running macOS 26.4 or newer.")
    if platform.machine() != "arm64":
        raise DeviceError("platform", "Use native arm64 Python on Apple Silicon; quit the Rosetta terminal and recreate .venv.")
    macos = platform.mac_ver()[0]
    try:
        parts = tuple(int(part) for part in macos.split(".")[:2])
        parts = (parts + (0, 0))[:2]
    except ValueError as exc:
        raise DeviceError("platform", "Could not detect the macOS version.", macos) from exc
    if parts < MIN_MACOS:
        raise DeviceError("platform", f"macOS {macos} detected. This tool requires macOS 26.4 or newer.")
    if installed_version() == "not installed":
        raise DeviceError("dependency", "pymobiledevice3 is missing. Activate .venv and run python -m pip install -U -r requirements.txt.")
    return macos


def pymobile_command(*args: str) -> list[str]:
    # Keep the venv interpreter path: resolving its symlink would lose the venv.
    return [sys.executable, "-m", "pymobiledevice3", *args]


async def run_pymobile(*args: str) -> str:
    env = dict(os.environ, NO_COLOR="1", TERM="dumb", COLUMNS="160")
    proc = await asyncio.create_subprocess_exec(
        *pymobile_command(*args), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), 45)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    text = re.sub(r"\x1b\[[0-9;]*m", "", output.decode(errors="replace"))
    if proc.returncode:
        raise DeviceError("cli_incompatible", "Installed pymobiledevice3 command checks failed. Run python -m pip install -U -r requirements.txt.", text)
    return text


async def inspect_installed_cli() -> dict[str, str]:
    commands = {
        "version": ("version",),
        "root": ("--help",),
        "location": ("developer", "dvt", "simulate-location", "--help"),
        "set": ("developer", "dvt", "simulate-location", "set", "--help"),
        "clear": ("developer", "dvt", "simulate-location", "clear", "--help"),
        "tunneld": ("remote", "tunneld", "--help"),
        "mount": ("mounter", "auto-mount", "--help"),
    }
    # Read-only commands; reap every child even if one check fails.
    results = await asyncio.gather(*(run_pymobile(*args) for args in commands.values()), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    help_text = dict(zip(commands, results))
    required = {
        "location": ("set", "clear"), "set": ("latitude", "longitude", "--tunnel"),
        "clear": ("--tunnel",), "mount": ("--tunnel",),
        "tunneld": ("--host", "--port", "--no-wifi", "--no-mobdev2", "--no-usbmux"),
    }
    for name, markers in required.items():
        if any(marker not in help_text[name] for marker in markers):
            raise DeviceError("cli_incompatible", f"The installed {name} command differs from the inspected API. Update pymobiledevice3 or check its GitHub issues.", help_text[name])
    return help_text


def load_api() -> SimpleNamespace:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
    from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService, auto_mount, image_type_for_device
    from pymobiledevice3.tunneld.api import get_tunneld_tunnels
    from pymobiledevice3.usbmux import list_devices

    for method in (create_using_usbmux, list_devices, auto_mount, get_tunneld_tunnels,
                   DvtProvider.connect, LocationSimulation.connect,
                   LocationSimulation.set, LocationSimulation.clear):
        if not inspect.iscoroutinefunction(method):
            raise DeviceError("api_incompatible", "The installed pymobiledevice3 API changed. Update it or check its GitHub issues.")
    return SimpleNamespace(**locals())


def tunnel_command(port: int) -> list[str]:
    # Native USB discovery only; usbmux monitoring can also find Wi-Fi devices.
    return pymobile_command("remote", "tunneld", "--host", "127.0.0.1", "--port", str(port),
                            "--no-wifi", "--no-mobdev2", "--no-usbmux")


def administrator_script(argv: list[str]) -> str:
    shell_command = "exec " + shlex.join(argv)
    escaped = shell_command.replace("\\", "\\\\").replace('"', '\\"')
    # The helper stays alive for the session, exceeding AppleScript's usual timeout.
    return f'with timeout of 2147483647 seconds\n do shell script "{escaped}" with administrator privileges\nend timeout'


def tunnel_start_error(detail: str) -> DeviceError:
    text = detail.lower()
    if ("operation not permitted" in text or "permission denied" in text) and any(
        folder in text for folder in ("/desktop/", "/documents/", "/downloads/")
    ):
        return DeviceError(
            "protected_folder",
            "macOS denied the administrator helper access to the project or Python environment "
            "in a protected folder. Move the checkout to ~/Projects/Spoofing-App, create and "
            "activate a fresh Python 3.12+ virtual environment there, install requirements, and retry.",
            detail,
        )
    return DeviceError("tunnel_not_running", "The tunnel did not start. Approve the macOS administrator dialog and retry.", detail)


class ManagedTunnel:
    """An elevated supervisor owns tunneld; control-socket EOF stops its child.

    Only this app's process group is stopped. No sudo, password handling,
    daemonization, PID files, or killing pre-existing tunnels is involved.
    """

    def __init__(self) -> None:
        self.port = 0
        self._directory: tempfile.TemporaryDirectory | None = None
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._result: asyncio.Task | None = None
        self._accepted = False

    async def start(self) -> None:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self._directory = tempfile.TemporaryDirectory(prefix="iphone-gps-", dir="/tmp")
        path = str(Path(self._directory.name) / "control.sock")
        connected = asyncio.get_running_loop().create_future()

        def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if connected.done():
                writer.close()
                return
            self._writer = writer
            self._accepted = True
            connected.set_result(reader)

        self._server = await asyncio.start_unix_server(accept, path=path)
        os.chmod(path, 0o600)
        # AppleScript elevation does not set sudo's ownership variables. Supply
        # them so the library's cache remains owned by the invoking user.
        argv = ["/usr/bin/env", f"SUDO_UID={os.getuid()}", f"SUDO_GID={os.getgid()}",
                f"SUDO_USER={getpass.getuser()}",
                sys.executable, str(Path(__file__).resolve()), "--supervise-tunnel", path, str(self.port)]
        self._proc = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript", "-e", administrator_script(argv),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        self._result = asyncio.create_task(self._proc.communicate())
        done, _ = await asyncio.wait((connected, self._result), timeout=120, return_when=asyncio.FIRST_COMPLETED)
        if connected not in done:
            detail = ""
            if self._result in done:
                detail = self._result.result()[0].decode(errors="replace")
            raise tunnel_start_error(detail)
        reader = connected.result()
        if await asyncio.wait_for(reader.readline(), 15) != b"started\n":
            raise DeviceError("tunnel_not_running", "The privileged tunnel helper did not start correctly.")

    def check_alive(self) -> None:
        if self._result is not None and self._result.done():
            output = self._result.result()[0].decode(errors="replace")
            raise DeviceError("tunnel_not_running", "The managed tunnel stopped. Retry and approve the administrator dialog.", output)

    async def connect_device(self, udid: str) -> None:
        """Request the selected USB device rather than relying on NCM discovery."""
        query = urlencode({"udid": udid, "connection_type": "usbmux"})

        def request() -> None:
            with urlopen(f"http://127.0.0.1:{self.port}/start-tunnel?{query}", timeout=45) as response:
                result = json.load(response)
            if not result.get("address") or not result.get("port"):
                raise DeviceError("tunnel_not_running", "The tunnel could not connect to the selected USB iPhone.", str(result))

        await asyncio.to_thread(request)

    async def close(self) -> None:
        if self._server:
            self._server.close()
        if self._writer:
            # EOF is sufficient, including when the main process crashes.
            self._writer.close()
            with suppress(OSError):
                await self._writer.wait_closed()
        if self._server:
            # Python 3.12 waits for accepted connections as well as the listener.
            # Close the control connection first so this cannot deadlock.
            await self._server.wait_closed()
        try:
            if self._result:
                try:
                    output, _ = await asyncio.wait_for(asyncio.shield(self._result), 25 if self._accepted else 2)
                except TimeoutError:
                    if self._proc and self._proc.returncode is None:
                        self._proc.kill()
                    await self._result
                    if self._accepted:
                        raise DeviceError("tunnel_cleanup", "Tunnel cleanup timed out. Check Activity Monitor for the managed tunneld process.")
                else:
                    if self._accepted and self._proc and self._proc.returncode:
                        raise DeviceError("tunnel_cleanup", "The tunnel helper reported a shutdown error.", output.decode(errors="replace"))
        finally:
            if self._directory:
                self._directory.cleanup()


def supervise_tunnel(control_path: str, port: int) -> int:
    """Internal root entry point. Connect before spawning to prevent orphan starts."""
    if platform.system() != "Darwin" or os.geteuid() != 0:
        raise RuntimeError("The tunnel supervisor requires macOS administrator authorization.")
    stopping = False

    def stop(_signal: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    with socket.socket(socket.AF_UNIX) as control, tempfile.TemporaryFile() as logs:
        control.settimeout(5)
        control.connect(control_path)
        # File-backed output avoids deadlocks; only its tail is exposed on error.
        child = subprocess.Popen(tunnel_command(port), stdout=logs, stderr=logs, start_new_session=True)
        failed = False
        try:
            control.sendall(b"started\n")
            while not stopping:
                if child.poll() is not None:
                    failed = True
                    break
                readable, _, _ = select.select([control], [], [], 0.5)
                if readable:
                    control.recv(32)
                    break  # Any control message or EOF requests shutdown.
        finally:
            if child.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
        if failed:
            logs.seek(0, os.SEEK_END)
            logs.seek(max(0, logs.tell() - 16384))
            print(logs.read().decode(errors="replace"), file=sys.stderr)
            return 1
    return 0


@dataclass
class DeviceStatus:
    connected: bool = False
    trusted: bool | None = None
    developer_mode: bool | None = None
    image_mounted: bool | None = None
    tunnel_running: bool = False
    udid: str | None = None
    ios_version: str | None = None
    name: str | None = None


class DeviceBackend:
    def __init__(self, udid: str | None = None) -> None:
        self.requested_udid = udid
        self.status = DeviceStatus()
        self.api: Any = None
        self.lockdown: Any = None
        self.rsd: Any = None
        self.location: Any = None
        self.active = False
        self.last_error: DeviceError | None = None
        self._stack = AsyncExitStack()
        self._tunnel: ManagedTunnel | None = None
        self._lock = asyncio.Lock()

    def _own_resource(self, resource: Any) -> None:
        async def close() -> None:
            await asyncio.wait_for(resource.close(), DEVICE_TIMEOUT)
        self._stack.push_async_callback(close)

    async def _open_service(self, service: Any) -> Any:
        # Own it before opening, including a cancelled or partial handshake.
        self._own_resource(service)
        await asyncio.wait_for(service.connect(), DEVICE_TIMEOUT)
        return service

    async def connect_usb(self) -> None:
        try:
            self.api = self.api or load_api()
            devices = [device for device in await asyncio.wait_for(self.api.list_devices(), DEVICE_TIMEOUT) if device.is_usb]
            if self.requested_udid:
                target = self.requested_udid.replace("-", "").lower()
                devices = [device for device in devices if device.serial.replace("-", "").lower() == target]
            if not devices:
                raise DeviceError("no_device", "No matching USB iPhone found. Connect and unlock it, then check Finder.")
            if len(devices) > 1:
                raise DeviceError("multiple_devices", "More than one USB device is connected. Select one with --udid.", "\n".join(device.serial for device in devices))
            self.status.connected = True
            self.status.udid = devices[0].serial
            self.lockdown = await asyncio.wait_for(self.api.create_using_usbmux(
                serial=devices[0].serial, connection_type="USB", autopair=False,
            ), DEVICE_TIMEOUT)
            self._own_resource(self.lockdown)
            self.status.ios_version = self.lockdown.product_version
            self.status.name = self.lockdown.all_values.get("DeviceName", "iPhone")
            if not self.lockdown.product_type.startswith("iPhone"):
                raise DeviceError("device_type", "Select an iPhone; this CLI targets iPhones only.")
            self.status.trusted = bool(self.lockdown.paired)
            if not self.status.trusted:
                raise DeviceError("not_trusted", "This Mac is not trusted. Unlock the iPhone, select it in Finder, and accept Trust on both devices.")
            if self.lockdown.all_values.get("DeviceLocked") is True:
                raise DeviceError("locked", "The iPhone is locked. Unlock it and keep the screen awake.")
            self.status.developer_mode = await asyncio.wait_for(self.lockdown.get_developer_mode_status(), DEVICE_TIMEOUT)
            await self._check_image(self.lockdown)
        except Exception as exc:
            self.last_error = friendly_error(exc, "USB connection")
            raise self.last_error from exc

    async def _check_image(self, provider: Any) -> None:
        async with self.api.MobileImageMounterService(provider) as mounter:
            self.status.image_mounted = await asyncio.wait_for(
                mounter.is_image_mounted(self.api.image_type_for_device(provider)), DEVICE_TIMEOUT,
            )

    async def _require_usb(self) -> None:
        devices = await asyncio.wait_for(self.api.list_devices(), DEVICE_TIMEOUT)
        if not any(device.is_usb and device.serial == self.status.udid for device in devices):
            self.status.connected = False
            raise DeviceError("no_device", "The iPhone was unplugged. Reconnect it and restart the session; unplugging resets simulated location.")

    async def prepare(self, *, mount: bool = False) -> None:
        try:
            if not self.lockdown:
                raise DeviceError("no_device", "Connect the USB iPhone first.")
            if not self.status.developer_mode:
                raise DeviceError("developer_mode_off", "Developer Mode is off. Enable Settings > Privacy & Security > Developer Mode, restart, and confirm.")
            if not mount and not self.status.image_mounted:
                raise DeviceError("image_not_mounted", "The developer image is not mounted. Run python app.py mount, then run Set again.")
            await self._require_usb()
            self._tunnel = ManagedTunnel()
            # Register cleanup BEFORE starting, including failures/cancellation.
            self._stack.push_async_callback(self._tunnel.close)
            print("Approve the macOS administrator dialog to start the USB tunnel.", flush=True)
            await self._tunnel.start()
            address = ("127.0.0.1", self._tunnel.port)
            async with asyncio.timeout(TUNNEL_TIMEOUT):
                requested = False
                while self.rsd is None:
                    self._tunnel.check_alive()
                    await self._require_usb()
                    try:
                        tunnels = await self.api.get_tunneld_tunnels(address)
                    except Exception as exc:
                        if type(exc).__name__ != "TunneldConnectionError":
                            raise
                        tunnels = {}
                    details = next((entries for udid, entries in tunnels.items()
                                    if udid.replace("-", "").lower() == self.status.udid.replace("-", "").lower()), [])
                    if details:
                        info = details[0]
                        rsd = self.api.RemoteServiceDiscoveryService((info["tunnel-address"], info["tunnel-port"]))
                        # Own it before connect so handshake failures don't leak.
                        self._own_resource(rsd)
                        await asyncio.wait_for(rsd.connect(), DEVICE_TIMEOUT)
                        self.rsd = rsd
                        self.status.tunnel_running = True
                    else:
                        if not requested:
                            # Wait for the daemon's HTTP listener before requesting
                            # a tunnel. USB presence is checked on every iteration.
                            try:
                                await self._tunnel.connect_device(self.status.udid)
                            except ConnectionRefusedError:
                                pass
                            except OSError as exc:
                                if not isinstance(getattr(exc, "reason", None), ConnectionRefusedError):
                                    raise
                            else:
                                requested = True
                        await asyncio.sleep(0.5)
            if mount and not self.status.image_mounted:
                try:
                    # This uses the selected device's RSD, including iOS 27 cryptex support.
                    await asyncio.wait_for(self.api.auto_mount(self.rsd), 300)
                except Exception as exc:
                    if type(exc).__name__ != "AlreadyMountedError":
                        raise
                # auto_mount returns after MountImage succeeds (or reports
                # AlreadyMountedError). Reopening the image-mounter immediately
                # can block while the library's mounting connection is alive.
                # Opening DVT below verifies that the mounted image is usable.
                self.status.image_mounted = True
            if not self.status.image_mounted:
                raise DeviceError("image_not_mounted", "The developer image is still unavailable. Check the mount error and update pymobiledevice3.")
            dvt = await self._open_service(self.api.DvtProvider(self.rsd))
            self.location = await self._open_service(self.api.LocationSimulation(dvt))
        except Exception as exc:
            self.last_error = friendly_error(exc, "developer service connection")
            raise self.last_error from exc

    async def set_location(self, latitude: float, longitude: float) -> None:
        lat, lng = validate_coordinates(latitude, longitude)
        async with self._lock:
            if self.location is None:
                raise DeviceError("session", "Open a location session before setting coordinates.")
            try:
                await self._require_usb()
                if self._tunnel:
                    self._tunnel.check_alive()
                # Set may reach the phone before its reply fails; shutdown must still clear.
                self.active = True
                await asyncio.wait_for(self.location.set(lat, lng), DEVICE_TIMEOUT)
                self.last_error = None
            except Exception as exc:
                self.last_error = friendly_error(exc, "Set location")
                raise self.last_error from exc

    async def clear_location(self) -> None:
        async with self._lock:
            if self.location is None:
                raise DeviceError("session", "Open a location session before clearing coordinates.")
            try:
                await asyncio.wait_for(self.location.clear(), DEVICE_TIMEOUT)
                self.active = False
                self.last_error = None
            except Exception as exc:
                self.last_error = friendly_error(exc, "Clear location")
                raise self.last_error from exc

    async def close(self) -> None:
        errors: list[DeviceError] = []
        try:
            if self.active:
                try:
                    await self.clear_location()
                except Exception as exc:
                    error = friendly_error(exc, "shutdown location clear")
                    errors.append(DeviceError(error.code,
                        "Shutdown could not confirm location was cleared. Unplug the iPhone to reset it. " + error.message,
                        error.detail))
        finally:
            try:
                await self._stack.aclose()  # location -> DVT -> RSD -> tunnel -> USB
            except Exception as exc:
                errors.append(friendly_error(exc, "session cleanup"))
            self.location = None
            self.rsd = None
            self.lockdown = None
            self.status.tunnel_running = False
        if errors:
            raise DeviceError("cleanup", "\n".join(error.message for error in errors),
                              "\n".join(error.detail for error in errors))


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--supervise-tunnel":
        raise SystemExit("Internal module. Run python app.py --help.")
    raise SystemExit(supervise_tunnel(sys.argv[2], int(sys.argv[3])))
