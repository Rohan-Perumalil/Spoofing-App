"""Stage 1 CLI. The FastAPI server and browser UI are deferred until tested."""

from __future__ import annotations

import argparse
import asyncio
import shlex
import signal
import sys
from contextlib import suppress

from device_backend import (
    DeviceBackend, DeviceError, friendly_error, inspect_installed_cli,
    installed_version, require_supported_host, validate_coordinates,
)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Simulate an iPhone location over USB on Apple Silicon macOS 26.4+.")
    cli.add_argument("--udid", help="Select a USB iPhone when more than one is connected.")
    cli.add_argument("--details", action="store_true", help="Expand technical error details.")
    commands = cli.add_subparsers(dest="command", required=True)
    set_command = commands.add_parser("set", help="Set coordinates and keep the session open until cleared/exited.")
    set_command.add_argument("latitude", type=float)
    set_command.add_argument("longitude", type=float)
    set_command.add_argument("--experimental-persist", action="store_true",
                             help="Show the unverified unplug workflow and allow detach without sending Clear.")
    commands.add_parser("clear", help="Clear simulated location, close the session, and stop its tunnel.")
    commands.add_parser("status", help="Check USB, trust, Developer Mode, and mounted image without elevation.")
    commands.add_parser("mount", help="Mount the developer image, check the location service, then exit.")
    commands.add_parser("doctor", help="Detect host/package versions and verify installed CLI help without elevation.")
    return cli


def show_error(error: DeviceError, details: bool) -> None:
    print(f"Error [{error.code}]: {error.message}", file=sys.stderr, flush=True)
    if error.detail:
        if details:
            print(f"Technical details:\n{error.detail}", file=sys.stderr)
        else:
            print("Technical details hidden. Use --details or type details in the active session.", file=sys.stderr)


def show_status(backend: DeviceBackend) -> None:
    labels = (("USB connected", "connected"), ("Trusted", "trusted"),
              ("Developer Mode", "developer_mode"), ("Image mounted", "image_mounted"),
              ("Tunnel running (this session)", "tunnel_running"))
    for label, field in labels:
        value = getattr(backend.status, field)
        print(f"  [{'yes' if value else 'no' if value is False else '?'}] {label}")
    print(f"  Location active (this session): {'yes' if backend.active else 'no'}", flush=True)


async def read_line() -> str:
    """Terminal input without a blocked executor that would prevent shutdown."""
    loop = asyncio.get_running_loop()
    line = loop.create_future()

    def ready() -> None:
        if not line.done():
            line.set_result(sys.stdin.readline())

    loop.add_reader(sys.stdin.fileno(), ready)
    try:
        return await line
    finally:
        loop.remove_reader(sys.stdin.fileno())


async def interactive_session(backend: DeviceBackend, args: argparse.Namespace) -> bool:
    persist = getattr(args, "experimental_persist", False)
    if not sys.stdin.isatty():
        print("Session active. Send SIGINT/SIGTERM to clear location and exit.", flush=True)
        await asyncio.Event().wait()
    print("Session open. Commands: set LAT LNG, clear, status, details, quit. Ctrl+C clears and exits.", flush=True)
    if persist:
        print("Experimental persistence (unverified on iOS 26.2):\n"
              "1. Verify the simulated location in Maps on the iPhone.\n"
              "2. On the iPhone, turn off Settings > Privacy & Security > Developer Mode.\n"
              "3. Unplug the cable, then type detach here.\n"
              "detach closes this session without sending Clear; it cannot guarantee persistence.\n"
              "To restore real GPS, restart the iPhone and verify Maps.\n"
              "To use this app again, enable Developer Mode and mount the image again.", flush=True)
    while True:
        print("location> ", end="", flush=True)
        line = await read_line()
        if not line:  # Terminal EOF follows the same cleanup path as quit.
            return False
        try:
            words = shlex.split(line)
            if not words:
                continue
            if words in (["quit"], ["exit"]):
                return False
            if words == ["detach"]:
                if not persist:
                    print("detach requires set --experimental-persist.", flush=True)
                elif not backend.active:
                    print("Set a location before using detach.", flush=True)
                else:
                    return True
                continue
            if len(words) == 3 and words[0] == "set":
                lat, lng = validate_coordinates(words[1], words[2])
                await backend.set_location(lat, lng)
                print(f"Location set: {lat}, {lng}", flush=True)
            elif words == ["clear"]:
                await backend.clear_location()
                print("Clear location sent. The session remains open for another Set.", flush=True)
            elif words == ["status"]:
                await backend._require_usb()
                if backend._tunnel:
                    backend._tunnel.check_alive()
                show_status(backend)
            elif words == ["details"]:
                print(backend.last_error.detail if backend.last_error else "No technical error details.", flush=True)
            else:
                print("Use set LAT LNG, clear, status, details, or quit.", flush=True)
        except (DeviceError, ValueError) as exc:
            error = friendly_error(exc, "session command")
            backend.last_error = error
            show_error(error, args.details)


async def run_command(args: argparse.Namespace) -> int:
    print(f"pymobiledevice3: {installed_version()}", flush=True)
    backend = DeviceBackend(args.udid)
    printed_ios = False
    result = 0
    detached = False
    try:
        macos = require_supported_host()
        print(f"macOS: {macos}; Python: {sys.version.split()[0]} (arm64)", flush=True)
        print("Checking installed pymobiledevice3 version and command help...", flush=True)
        checks = await inspect_installed_cli()
        print(f"CLI checks passed (version {checks['version'].strip()}).", flush=True)
        if args.command == "doctor":
            print("iOS: not queried (doctor performs no device operations).", flush=True)
            printed_ios = True
        else:
            await backend.connect_usb()
            print(f"iOS: {backend.status.ios_version}; device: {backend.status.name}; UDID: {backend.status.udid}", flush=True)
            printed_ios = True
            if args.command == "status":
                show_status(backend)
            else:
                await backend.prepare(mount=args.command == "mount")
                if args.command == "mount":
                    print("Developer image mounted; location service opened successfully.", flush=True)
                elif args.command == "clear":
                    await backend.clear_location()
                    print("Clear location sent.", flush=True)
                else:
                    await backend.set_location(args.latitude, args.longitude)
                    print(f"Location set: {args.latitude}, {args.longitude}", flush=True)
                    show_status(backend)
                    detached = await interactive_session(backend, args)
    except Exception as exc:
        result = 1
        show_error(friendly_error(exc, args.command), args.details)
    finally:
        if not printed_ios:
            print(f"iOS: {backend.status.ios_version or 'unavailable (no device session)'}", flush=True)
        try:
            was_active = backend.active
            # A signal arriving during cleanup must not cancel the Clear request.
            cleanup = asyncio.create_task(backend.close(clear_location=not detached))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            if detached:
                print("Detached: no Clear request sent; Mac session and managed tunnel closed. "
                      "Check Maps after disconnecting. Persistence is unverified.", flush=True)
            elif was_active:
                print("Shutdown: Clear location sent; session closed and managed tunnel stopped.", flush=True)
        except Exception as exc:
            result = 1
            show_error(friendly_error(exc, "shutdown"), args.details)
    return result


async def main_async(args: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    for signum in signals:
        loop.add_signal_handler(signum, stopped.set)
    operation = asyncio.create_task(run_command(args))
    interrupted = asyncio.create_task(stopped.wait())
    try:
        done, _ = await asyncio.wait((operation, interrupted), return_when=asyncio.FIRST_COMPLETED)
        if operation in done:
            return operation.result()
        print("\nShutting down: clearing location and stopping the managed tunnel...", flush=True)
        operation.cancel()
        with suppress(asyncio.CancelledError):
            await operation  # Its finally block finishes before asyncio.run exits.
        return 130
    finally:
        interrupted.cancel()
        with suppress(asyncio.CancelledError):
            await interrupted
        for signum in signals:
            loop.remove_signal_handler(signum)


def main() -> int:
    cli = parser()
    args = cli.parse_args()
    if args.command == "set":
        try:
            args.latitude, args.longitude = validate_coordinates(args.latitude, args.longitude)
        except DeviceError as exc:
            cli.error(exc.message)
    # Validate before installing macOS-only event-loop signal handlers.
    if sys.platform != "darwin":
        print(f"pymobiledevice3: {installed_version()}\niOS: unavailable (macOS required)", flush=True)
        show_error(DeviceError("platform", "Run this CLI on an Apple Silicon Mac with macOS 26.4 or newer."), args.details)
        return 1
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
