"""Lifecycle and validation tests; no USB device or elevation is used."""

import asyncio
import io
import os
import signal
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import app
from device_backend import (
    DeviceBackend, DeviceError, ManagedTunnel, administrator_script, friendly_error,
    require_supported_host, supervise_tunnel, tunnel_command, tunnel_start_error, validate_coordinates,
)


class ValidationTests(unittest.TestCase):
    def test_boundaries_and_non_finite_coordinates(self):
        self.assertEqual(validate_coordinates(-90, 180), (-90, 180))
        self.assertEqual(validate_coordinates(90, -180), (90, -180))
        for coordinates in ((90.01, 0), (0, -180.01), (float("nan"), 0),
                            (0, float("inf")), ("bad", 0), (None, 0)):
            with self.subTest(coordinates=coordinates), self.assertRaises(DeviceError):
                validate_coordinates(*coordinates)

    def test_platform_and_runtime_version_gates(self):
        with patch("device_backend.platform.system", return_value="Windows"):
            with self.assertRaisesRegex(DeviceError, "Apple Silicon"):
                require_supported_host()
        with patch("device_backend.platform.system", return_value="Darwin"), \
             patch("device_backend.platform.machine", return_value="arm64"), \
             patch("device_backend.installed_version", return_value="11.27.2"):
            for macos, supported in (("26.3", False), ("26.4", True), ("27.0", True)):
                with self.subTest(macos=macos), patch("device_backend.platform.mac_ver", return_value=(macos, (), "")):
                    if supported:
                        self.assertEqual(require_supported_host(), macos)
                    else:
                        with self.assertRaises(DeviceError):
                            require_supported_host()
            with patch("device_backend.platform.machine", return_value="x86_64"):
                with self.assertRaisesRegex(DeviceError, "Rosetta"):
                    require_supported_host()

    def test_error_classification_and_hidden_details(self):
        for name, code in (("NoDeviceConnectedError", "no_device"), ("PasswordRequiredError", "locked"),
                           ("NotPairedError", "not_trusted"), ("DeveloperModeIsNotEnabledError", "developer_mode_off"),
                           ("NotMountedError", "image_not_mounted"), ("TunneldConnectionError", "tunnel_not_running"),
                           ("InvalidServiceError", "unsupported_service"), ("DeviceFeatureNotSupportedError", "unsupported_service")):
            with self.subTest(name=name):
                error = friendly_error(type(name, (Exception,), {})("diagnostic"))
                self.assertEqual(error.code, code)
                self.assertIn("diagnostic", error.detail)
        unsupported = friendly_error(type("InvalidServiceError", (Exception,), {})())
        self.assertIn("pip install -U pymobiledevice3", unsupported.message)
        self.assertIn("github.com/doronz88/pymobiledevice3/issues", unsupported.message)
        with patch("sys.stderr", new_callable=io.StringIO) as output:
            app.show_error(DeviceError("unexpected", "Readable message", "private detail"), False)
        self.assertNotIn("private detail", output.getvalue())
        self.assertEqual(friendly_error(RuntimeError()).code, "unexpected")

    def test_privileged_command_is_quoted_and_usb_only(self):
        script = administrator_script(["/path with spaces/python", "file;name.py", "a'quote", 'a"quote', "$(echo unsafe)"])
        self.assertIn("with administrator privileges", script)
        self.assertIn("'/path with spaces/python'", script)
        self.assertIn("'$(echo unsafe)'", script)
        command = tunnel_command(49152)
        for arg in ("127.0.0.1", "49152", "--no-wifi", "--no-mobdev2", "--no-usbmux"):
            self.assertIn(arg, command)
        self.assertNotIn("--daemonize", command)

    def test_protected_folder_permission_error_explains_recovery(self):
        for folder in ("Desktop", "Documents", "Downloads"):
            detail = f"PermissionError: [Errno 1] Operation not permitted: '/Users/test/{folder}/app/.venv312/pyvenv.cfg' (1)"
            with self.subTest(folder=folder):
                error = tunnel_start_error(detail)
                self.assertEqual(error.code, "protected_folder")
                self.assertIn("~/Projects/Spoofing-App", error.message)
                self.assertIn("fresh Python 3.12+ virtual environment", error.message)
                self.assertEqual(error.detail, detail)
        self.assertEqual(tunnel_start_error("User canceled. (-128)").code, "tunnel_not_running")
        self.assertEqual(tunnel_start_error("").code, "tunnel_not_running")

    def test_supervisor_eof_stops_only_its_child(self):
        control = Mock()
        control.recv.return_value = b""
        control.__enter__ = Mock(return_value=control)
        control.__exit__ = Mock(return_value=False)
        child = Mock(pid=1234)
        child.poll.return_value = None
        with patch("device_backend.platform.system", return_value="Darwin"), \
             patch("device_backend.os.geteuid", return_value=0, create=True), \
             patch("device_backend.signal.signal"), \
             patch("device_backend.signal.SIGHUP", 1, create=True), \
             patch("device_backend.socket.socket", return_value=control), \
             patch("device_backend.socket.AF_UNIX", 1, create=True), \
             patch("device_backend.tempfile.TemporaryFile", return_value=io.BytesIO()), \
             patch("device_backend.subprocess.Popen", return_value=child) as launch, \
             patch("device_backend.select.select", return_value=([control], [], [])), \
             patch("device_backend.os.killpg", create=True) as kill:
            self.assertEqual(supervise_tunnel("/tmp/test/control.sock", 49152), 0)
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        control.connect.assert_called_once_with("/tmp/test/control.sock")
        kill.assert_called_once_with(1234, signal.SIGTERM)
        child.wait.assert_called_once_with(timeout=10)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == "posix", "Unix control sockets require POSIX")
    async def test_tunnel_close_releases_accepted_control_connection(self):
        # A real accepted socket catches the Python 3.12 server.wait_closed
        # deadlock that mocks of server.close cannot reproduce.
        tunnel = ManagedTunnel()
        connected = asyncio.get_running_loop().create_future()

        def accept(reader, writer):
            tunnel._writer = writer
            connected.set_result(None)

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = directory + "/control.sock"
            tunnel._server = await asyncio.start_unix_server(accept, path)
            reader, writer = await asyncio.open_unix_connection(path)
            try:
                await connected
                await asyncio.wait_for(tunnel.close(), 2)
                self.assertEqual(await reader.read(), b"")
            finally:
                writer.close()
                await writer.wait_closed()

    def make_backend(self):
        backend = DeviceBackend()
        backend.status.udid = "PHONE"
        backend.api = SimpleNamespace(list_devices=AsyncMock(return_value=[SimpleNamespace(is_usb=True, serial="PHONE")]))
        backend.location = SimpleNamespace(set=AsyncMock(), clear=AsyncMock())
        backend._tunnel = SimpleNamespace(check_alive=Mock())
        return backend

    async def test_clear_precedes_all_transport_teardown(self):
        backend = self.make_backend()
        events = []

        async def record(name):
            events.append(name)

        async def clear():
            await record("clear")

        backend.location.clear.side_effect = clear
        for name in ("usb", "tunnel", "rsd", "dvt", "location"):
            backend._stack.push_async_callback(record, name)
        await backend.set_location(37.3318, -122.0312)
        self.assertTrue(backend.active)
        self.assertEqual(events, [])
        await backend.close()
        self.assertEqual(events, ["clear", "location", "dvt", "rsd", "tunnel", "usb"])
        self.assertFalse(backend.active)
        await backend.close()

    async def test_clear_failure_still_closes_every_resource(self):
        backend = self.make_backend()
        backend.active = True
        backend.location.clear.side_effect = ConnectionError("unplugged")
        closed = AsyncMock()
        backend._stack.push_async_callback(closed)
        with self.assertRaisesRegex(DeviceError, "Unplug the iPhone"):
            await backend.close()
        closed.assert_awaited_once()

    async def test_failed_set_still_attempts_clear(self):
        backend = self.make_backend()
        backend.location.set.side_effect = TimeoutError()
        clear = backend.location.clear
        with self.assertRaises(DeviceError):
            await backend.set_location(1, 2)
        self.assertTrue(backend.active)
        await backend.close()
        clear.assert_awaited_once()

    async def test_clear_keeps_session_and_allows_another_set(self):
        backend = self.make_backend()
        location = backend.location
        await backend.set_location(1, 2)
        await backend.clear_location()
        self.assertFalse(backend.active)
        self.assertIs(backend.location, location)
        await backend.set_location(3, 4)
        self.assertTrue(backend.active)
        await backend.close()

    async def test_unplug_rejected_before_another_set(self):
        backend = self.make_backend()
        backend.api.list_devices.return_value = [SimpleNamespace(is_usb=False, serial="PHONE")]
        with self.assertRaises(DeviceError) as caught:
            await backend.set_location(1, 2)
        self.assertEqual(caught.exception.code, "no_device")
        backend.location.set.assert_not_awaited()
        self.assertFalse(backend.active)

    async def test_tunnel_cleanup_failure_does_not_skip_usb_close(self):
        backend = self.make_backend()
        closed = AsyncMock()
        failed = AsyncMock(side_effect=RuntimeError("tunnel cleanup failed"))
        backend._stack.push_async_callback(closed)
        backend._stack.push_async_callback(failed)
        with self.assertRaises(DeviceError):
            await backend.close()
        closed.assert_awaited_once()

    async def test_no_device_and_multiple_devices(self):
        for devices, code in (([], "no_device"),
                              ([SimpleNamespace(is_usb=True, serial="A"), SimpleNamespace(is_usb=True, serial="B")], "multiple_devices")):
            backend = DeviceBackend()
            backend.api = SimpleNamespace(list_devices=AsyncMock(return_value=devices))
            with self.assertRaises(DeviceError) as caught:
                await backend.connect_usb()
            self.assertEqual(caught.exception.code, code)
            await backend.close()

    async def test_partial_service_handshake_is_closed(self):
        backend = self.make_backend()
        service = SimpleNamespace(connect=AsyncMock(side_effect=TimeoutError()), close=AsyncMock())
        with self.assertRaises(TimeoutError):
            await backend._open_service(service)
        await backend.close()
        service.close.assert_awaited_once()

    async def test_failed_tunnel_start_is_still_closed(self):
        backend = self.make_backend()
        backend.lockdown = SimpleNamespace(close=AsyncMock())
        backend.status.developer_mode = True
        backend.status.image_mounted = True
        tunnel = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("startup failure")), close=AsyncMock())
        with patch("device_backend.ManagedTunnel", return_value=tunnel), patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(DeviceError):
                await backend.prepare()
        await backend.close()
        tunnel.close.assert_awaited_once()

    async def test_signal_cancellation_waits_for_location_cleanup(self):
        backend = self.make_backend()
        clearing, continue_clear = asyncio.Event(), asyncio.Event()

        async def clear():
            clearing.set()
            await continue_clear.wait()

        backend.location.clear.side_effect = clear
        backend.active = True
        args = SimpleNamespace(command="doctor", details=False, udid=None)
        with patch("app.DeviceBackend", return_value=backend), \
             patch("app.require_supported_host", return_value="26.4"), \
             patch("app.inspect_installed_cli", new=AsyncMock(return_value={"version": "11.27.2"})), \
             patch("sys.stdout", new_callable=io.StringIO):
            running = asyncio.create_task(app.run_command(args))
            await clearing.wait()
            running.cancel()
            await asyncio.sleep(0)
            self.assertFalse(running.done())
            continue_clear.set()
            with self.assertRaises(asyncio.CancelledError):
                await running
        self.assertFalse(backend.active)


if __name__ == "__main__":
    unittest.main()
