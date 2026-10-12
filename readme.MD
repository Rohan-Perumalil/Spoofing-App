# USB iPhone location spoofer

A macOS CLI that sets and clears a simulated GPS location on a USB-connected
iPhone using the pymobiledevice3 Python API. Mounting and setting a location have
been tested on the Mac and iPhone described below. A browser map and route mode
are planned; the current app runs in Terminal.

## Everyday use: set Cupertino

Your working copy is in **`~/Projects/Spoofing-App`**, with the Python environment
**`.venv-mac`**. Open that folder in VS Code if you want to edit the app.

1. Connect your iPhone to the Mac with a USB data cable and unlock it.
2. Open Terminal or the VS Code terminal and run:

   ```sh
   cd "$HOME/Projects/Spoofing-App" &&
   source .venv-mac/bin/activate &&
   python app.py --details set -- 37.3318 -122.0312
   ```

3. Approve the macOS administrator dialog using your Mac password.
4. Wait for `Location set: 37.3318, -122.0312` and the `location>` prompt.
5. Open Maps on the iPhone and tap its current-location button to verify Cupertino.

Keep the terminal session open and the phone connected for reliable operation.
If Set reports that the developer image is not mounted, run these commands
separately, waiting for Mount to succeed before running Set:

```sh
python app.py --details mount
python app.py --details set -- 37.3318 -122.0312
```

### Change or restore the location

At `location>`, enter one of these commands and press Return. These are app
commands, so do not prefix them with `python app.py`:

| Command | What it does |
| --- | --- |
| `set 40.7128 -74.0060` | Change to New York City. |
| `set 37.3318 -122.0312` | Change back to Cupertino. |
| `clear` | Send the request to restore real GPS; keep the session open. |
| `status` | Show connection and session status. |
| `details` | Show details of the last error. |
| `quit` | Clear the simulated location, close the session, and stop its tunnel. |

Ctrl+C also runs shutdown cleanup. Check Maps to confirm your real location
returns before disconnecting. To start again later, repeat the Everyday use
commands above; you do not need to reinstall dependencies each time.

### If you unplug the iPhone

The app currently uses USB and cannot send further commands while disconnected.
The phone may briefly or sometimes continue showing the simulated position, but
the duration and reliability of that behavior are **not verified**. A position
displayed in Maps immediately after unplugging is not proof of lasting simulation.
The app does not guarantee operation without a connection to the Mac.

To clear after unplugging, reconnect and unlock the phone, then try `clear` in the
active session. If that session reports a connection error, exit it and run:

```sh
cd "$HOME/Projects/Spoofing-App" &&
source .venv-mac/bin/activate &&
python app.py --details clear
```

Approve the administrator dialog and verify the result in Maps. If Set or Clear
fails, keep the full `--details` output for troubleshooting.

## Requirements

- Apple Silicon Mac running **macOS 26.4 or newer**, following your clarified
  minimum instead of the original macOS 27 requirement.
- Native **arm64 Python 3.12 or newer**, outside Rosetta.
- USB-connected iPhone. Your reported version is **iOS 26.2**; the app detects the
  actual iOS and macOS versions at runtime.
- Mac trusted by the iPhone, and Developer Mode enabled on the iPhone.
- Administrator authorization to start the managed USB tunnel.

## File structure

```text
app.py                 Stage 1 CLI; future FastAPI entry point
device_backend.py      Device API, command checks, and tunnel lifecycle
requirements.txt       Unpinned pymobiledevice3 dependency
static/                Reserved for the later Leaflet/OSM UI
tests/test_stage1.py    Hardware-free validation and lifecycle tests
readme.MD
```

## Setup on your Mac

Place the project in **`~/Projects/Spoofing-App`** on your Mac. Exclude the Windows `.venv`,
`.tmp`, and `__pycache__` folders; create a fresh virtual environment on the Mac.

macOS protects Desktop, Documents, and Downloads. An administrator helper can
be denied access to a Python environment in those folders. Use the Projects
location for both the checkout and its virtual environment.

Open a native Terminal and run:

```sh
cd "$HOME/Projects/Spoofing-App"
/opt/homebrew/bin/python3.12 --version
/opt/homebrew/bin/python3.12 -c 'import platform; print(platform.machine(), platform.mac_ver()[0])'
/opt/homebrew/bin/python3.12 -m venv .venv-mac
source .venv-mac/bin/activate
python -m pip install -U -r requirements.txt
python app.py doctor
```

These commands use the Python 3.12 installation verified on this Mac. On another
Mac, substitute the path to its native Python 3.12+ interpreter. If a command
fails, stop and resolve it before continuing. Do not install into Conda `(base)`.

The architecture check must print `arm64`, and Python must be 3.12 or newer.
`doctor` detects the host/package versions and verifies installed command help
without accessing the iPhone or requesting administrator privileges.

Each app invocation checks the installed pymobiledevice3 version and command help
before device operations. You can inspect those commands directly:

```sh
pymobiledevice3 version
pymobiledevice3 --help
pymobiledevice3 developer dvt simulate-location --help
pymobiledevice3 developer dvt simulate-location set --help
pymobiledevice3 developer dvt simulate-location clear --help
pymobiledevice3 remote tunneld --help
pymobiledevice3 mounter auto-mount --help
```

Connect the iPhone with a data cable and unlock it. Select it in Finder and accept
Trust on both devices. Enable **Settings > Privacy & Security > Developer Mode**,
restart the iPhone, and confirm the on-device prompt. Keep its screen awake during
setup.

## Test stage 1

With the virtual environment activated, run:

```sh
python app.py status
python app.py mount
python app.py set -- 37.3318 -122.0312
```

`status` checks USB connection, trust, Developer Mode, and the developer image.
It does not start a tunnel or elevate. Its tunnel field describes this session,
so it displays `no` until a Set/Clear/Mount invocation starts one. Unknown fields
display `?`.

`mount` starts its tunnel, uses the library's `auto_mount` API if the image is not
already mounted, checks the location service, then closes its connections and tunnel.
Image downloads and personalization may require network access and use the
library's usual cache outside this project.

Mount and Set request a macOS administrator dialog. Approve it when prompted.
Set prints the detected iOS/library versions and keeps the device session open.
Keep Terminal running and check your position in the iPhone's Maps app; the example
coordinates are in Cupertino.

At the `location>` prompt, type:

```text
clear
```

Verify Maps returns to your real location. Clear leaves the session open for
another Set. To test shutdown cleanup, type:

```text
set 40.7128 -74.0060
```

Check the simulated position, then press **Ctrl+C**. Verify the real location
returns and the app reports that it closed the session and stopped its tunnel.

Available interactive commands:

```text
set LAT LNG
clear
status
details
quit
```

`quit`, `exit`, Ctrl+C, terminal EOF, SIGTERM, and SIGHUP run shutdown cleanup.
With noninteractive stdin, Set stays alive until a signal. Latitude must be a
finite number from -90 to 90; longitude must be from -180 to 180. NaN and infinity
are rejected before device access.

An independent Clear invocation is available:

```sh
python app.py clear
```

Prefer `clear` in the active prompt while that session is open. If multiple USB
devices are connected, select the iPhone explicitly:

```sh
python app.py --udid YOUR_IPHONE_UDID set -- 37.3318 -122.0312
```

## Session and tunnel lifecycle

The app keeps its USB, RSD, DVT, and location connections open while a location is
active. It sends Clear before closing those resources on graceful shutdown.
After unplugging, persistence is not guaranteed; see the usage instructions above.
If shutdown cannot reach the device, it reports the failure. Reconnect and send
Clear to restore real GPS explicitly. Force-kill and power loss cannot send a
Clear request.

Run the app as your normal user. It starts an elevated tunnel supervisor through
`osascript` using `do shell script ... with administrator privileges`. The app
never receives, stores, or logs the administrator password.

The managed tunneld binds to `127.0.0.1` on an available port. Wi-Fi, mobdev2, and
usbmux monitoring are disabled. If native USB discovery finds no tunnel, the app
requests a usbmux tunnel for the selected, USB-connected iPhone explicitly.
The elevated helper supplies the invoking user's ownership information so the
library's cache stays writable by the normal user. The supervisor
connects to a private Unix socket before launching tunneld and stops its own child
process group when the app closes the socket or exits. Normal shutdown needs no
second administrator dialog. Existing tunnels are not reused or stopped.

All subprocess calls are centralized in `device_backend.py`; setting, clearing,
and image mounting use the Python API.

## Manual reference commands

These diagnostic examples are for you to run on the Mac after checking its
installed help. No sudo or privileged device commands were executed during
development.

```sh
# Terminal 1: foreground daemon. Stop it yourself with Ctrl+C when finished.
sudo "$VIRTUAL_ENV/bin/python" -m pymobiledevice3 remote tunneld \
  --host 127.0.0.1 --port 49151 --no-wifi --no-mobdev2 --no-usbmux

# Terminal 2: activate the same virtual environment and replace the UDID.
python -m pymobiledevice3 mounter auto-mount --tunnel YOUR_IPHONE_UDID:49151
python -m pymobiledevice3 developer dvt simulate-location set \
  --tunnel YOUR_IPHONE_UDID:49151 -- 37.3318 -122.0312

# Terminal 3: activate the same environment and clear while Set stays open.
python -m pymobiledevice3 developer dvt simulate-location clear \
  --tunnel YOUR_IPHONE_UDID:49151
```

## Troubleshooting

| Error | Next action |
| --- | --- |
| No device | Check the data cable, unlock the iPhone, and check Finder. Wi-Fi-only devices are excluded. |
| Locked | Unlock the iPhone and keep its screen awake during setup. |
| Not trusted | Select the iPhone in Finder and accept Trust on both devices. |
| Developer Mode off | Enable it in Privacy & Security, restart, and confirm. |
| Image not mounted | Run `python app.py mount`; inspect any download or personalization error. |
| Tunnel not running | Retry and approve the administrator dialog. Close competing device tools if connection resets persist. |
| Protected folder / `Operation not permitted` reading `pyvenv.cfg` | Move the checkout to `~/Projects/Spoofing-App` and create a fresh virtual environment there. |
| Unsupported service | Run `pip install -U pymobiledevice3` and check [upstream GitHub issues](https://github.com/doronz88/pymobiledevice3/issues). |
| Other failure | Expand technical details using `--details` or the interactive `details` command. |

For example:

```sh
python app.py --details mount
python app.py --details set -- 37.3318 -122.0312
```

Global options such as `--details` and `--udid` go before the command. Technical
details are hidden by default. A successful Clear means the library sent the stop
request; it does not return an acknowledged real-GPS position. Verify the result
on the iPhone.

If you initially cloned under Desktop and see `Operation not permitted` while
the administrator helper reads `pyvenv.cfg`, move the checkout into Projects
using Finder. Then recreate the environment rather than activating the moved one:

```sh
cd "$HOME/Projects/Spoofing-App"
/opt/homebrew/bin/python3.12 -m venv .venv-mac
source .venv-mac/bin/activate
python -m pip install -U -r requirements.txt
python app.py --details mount
```

## Verification and pending work

The implementation was inspected against **pymobiledevice3 11.27.2** on a Windows
x64 development host with Python 3.13.7. Root, location group, Set, Clear, and
tunneld help ran there. Full Windows dependency installation failed because
`lzfse` requires a C++ compiler; inspection dependencies were installed separately.
Mount help could not load in that Windows environment because the compression
module was absent. The Mac's `doctor` command checks it again.

The current 17 hardware-free validation/lifecycle tests passed on this Mac.
Run them with:

```sh
python -m unittest discover -s tests -v
```

Verified on Apple Silicon macOS **26.5.1**, Python **3.12.15**, pymobiledevice3
**11.27.2**, and iOS **26.2**: host checks, USB discovery, trust and Developer Mode
checks, administrator-authorized tunnel startup, image mounting, location-service
access, and successful Set. The user confirmed Cupertino in Maps. The corrected
Mount command exited cleanly. Real-GPS restoration after Clear and reliable
persistence after unplugging still need confirmation on the phone.

After stage 1 passes, stage 2 adds FastAPI bound only to `127.0.0.1`, stage 3 adds
the Leaflet/OpenStreetMap UI, and stage 4 adds waypoints, speed, and Start/Pause/Stop.

Upstream references: [Python API guide](https://doronz88.github.io/pymobiledevice3/guides/python-api/)
and [tunnel guide](https://doronz88.github.io/pymobiledevice3/guides/ios17-tunnels/).
