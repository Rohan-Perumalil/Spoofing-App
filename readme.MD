# USB iPhone location simulator — stage 1

Stage 1 implements a CLI that sets and clears simulated GPS location with the
pymobiledevice3 Python API. Test this on the iPhone before adding the FastAPI
server, map UI, or route mode.

## Structure and assumptions

```text
app.py                 CLI now; FastAPI entry point in stage 2
device_backend.py      Device API, installed-command checks, tunnel lifecycle
requirements.txt       Unpinned pymobiledevice3
static/                Reserved for stage 3 (only .gitkeep for now)
tests/test_stage1.py    Hardware-free validation and lifecycle checks
README.md
```

- Apple Silicon, native arm64 Python 3.12+, macOS **26.4 or newer**, following
  your clarification instead of the original macOS 27 requirement.
- Reported iPhone version: **iOS 26.2**. Actual iOS and macOS versions are detected
  at runtime. One USB-connected iPhone, or an explicit `--udid` if several are
  connected. Trust the Mac in Finder and enable Developer Mode first.
- Set keeps the USB, RSD, DVT, and location channel open. Clear sends the device's
  stop-simulation request. Graceful shutdown clears before closing the session
  and stopping its managed tunnel.
- **The location resets on unplug.** If shutdown cannot reach the device, the CLI
  reports the failure and tells you to unplug it. Force-kill and power loss cannot
  send a Clear request.
- Stage 1 has no external HTTP server or UI.

## Setup on your Mac

Run from this project directory in a native terminal:

```sh
python3 --version
python3 -c 'import platform; print(platform.machine(), platform.mac_ver()[0])'
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U -r requirements.txt
pymobiledevice3 version
pymobiledevice3 --help
pymobiledevice3 developer dvt simulate-location --help
pymobiledevice3 developer dvt simulate-location set --help
pymobiledevice3 developer dvt simulate-location clear --help
pymobiledevice3 remote tunneld --help
pymobiledevice3 mounter auto-mount --help
python app.py doctor
```

The architecture check must print `arm64`. Each app invocation checks the installed
CLI version/help before device operations and rejects incompatible command/API
shapes. `doctor` checks commands without touching the iPhone or requesting
administrator privileges. Coordinate validation happens before device access.

Connect the iPhone with a data cable, unlock it, and select it in Finder. Accept
the Trust prompt on both devices. Enable **Settings > Privacy & Security >
Developer Mode**, restart the iPhone, and confirm the on-device prompt.

## Test stage 1

```sh
python app.py status
python app.py mount
python app.py set -- 37.3318 -122.0312
```

`status` does not elevate or start a tunnel. Its tunnel field describes this
session, so it is `no` until Set/Clear/Mount starts one. Unknown checklist fields
are shown as `?`.

`mount` uses the library's `auto_mount` API for the selected device, downloads a
developer image if needed, opens the location service to check readiness, then
closes its connections and tunnel. Downloads/personalization may require network
access and use pymobiledevice3's usual cache outside this project.

Set displays the detected library/iOS versions and an interactive prompt:

```text
location> set 40.7128 -74.0060
location> clear
location> set 37.3318 -122.0312
location> status
location> quit
```

Check the position in Maps after Set and after Clear. `clear` restores real GPS
and leaves the session available for another Set. `quit`, Ctrl+C, terminal EOF,
SIGTERM, and SIGHUP use shutdown cleanup. With noninteractive stdin, Set stays
alive until a signal. Only finite latitude -90..90 and longitude -180..180 are
accepted; NaN and infinity are rejected.

An independent Clear invocation is also available:

```sh
python app.py clear
```

Prefer `clear` in the active prompt when that session is open. To select a device:

```sh
python app.py --udid YOUR_IPHONE_UDID set -- 37.3318 -122.0312
```

## Administrator rights and tunnel ownership

Run the app as your normal user. Mount/Set/Clear request a macOS administrator
dialog through `osascript` and `do shell script ... with administrator privileges`.
Only the tunnel supervisor and its child run elevated. The app never receives,
stores, or logs the password. It binds tunneld to `127.0.0.1` on a private available
port and disables Wi-Fi, mobdev2, and usbmux discovery, retaining native USB
discovery. It does not reuse or stop a pre-existing tunneld.

The privileged supervisor connects to a private Unix socket **before** launching
tunneld. It stops its own child process group when the app closes the socket or
exits, with SIGTERM followed by SIGKILL only if necessary. Normal shutdown does
not require a second administrator dialog. Cleanup failures are reported.

No sudo or privileged device commands were executed during development. To
verify the raw reference commands yourself, run these on your Mac. They derive
from installed 11.27.2 help/source; Mac mount help is still to be checked by `doctor`:

```sh
# Terminal 1: foreground daemon; stop it yourself with Ctrl+C when finished.
sudo "$VIRTUAL_ENV/bin/python" -m pymobiledevice3 remote tunneld \
  --host 127.0.0.1 --port 49151 --no-wifi --no-mobdev2 --no-usbmux

# Terminal 2: same virtual environment. Replace YOUR_IPHONE_UDID.
python -m pymobiledevice3 mounter auto-mount --tunnel YOUR_IPHONE_UDID:49151
python -m pymobiledevice3 developer dvt simulate-location set \
  --tunnel YOUR_IPHONE_UDID:49151 -- 37.3318 -122.0312

# Terminal 3: while Set stays open, clear the simulation.
python -m pymobiledevice3 developer dvt simulate-location clear \
  --tunnel YOUR_IPHONE_UDID:49151
```

These manual commands are diagnostic examples. The app sets, clears, and mounts
through the Python API; every subprocess is centralized in `device_backend.py`.

## Troubleshooting

| Error | Next action |
| --- | --- |
| No device | Check data cable, unlock the iPhone, and check Finder. Wi-Fi-only devices are excluded. |
| Locked | Unlock it and keep the screen awake during setup. |
| Not trusted | Select the iPhone in Finder and accept Trust on both devices. |
| Developer Mode off | Enable it in Privacy & Security, restart, and confirm. |
| Image not mounted | Run `python app.py mount`; check download/personalization errors. |
| Tunnel not running | Retry and approve the administrator dialog. Close competing device tools if connection resets persist. |
| Unsupported service | Run `pip install -U pymobiledevice3`; check [upstream issues](https://github.com/doronz88/pymobiledevice3/issues). |
| Other failure | Run `python app.py --details ...`, or type `details` in an active session. |

Technical details are hidden by default and exposed on demand. Structured
`DeviceError` also carries a separate detail field for the collapsible panel in
stage 3. A successful Clear means the library sent the stop request; the protocol
does not return an acknowledged real-GPS position. Verify it on the iPhone.

## Verification and pending work

Development host: Windows x64, Python 3.13.7. The latest package installed for
inspection reports **pymobiledevice3 11.27.2**. Root help and the location group,
Set, Clear, and tunneld help ran here. The full Windows dependency install failed
because `lzfse` requires a C++ compiler; command inspection dependencies were
installed separately. Mount help cannot load here because that compression
module is absent. Create a fresh Mac virtual environment; the ignored Windows
`.venv` must not be copied to the Mac.

Installed source was inspected for async `create_using_usbmux`,
`get_tunneld_tunnels`, `RemoteServiceDiscoveryService`, `DvtProvider`,
`LocationSimulation.set/clear`, and `auto_mount`. See the upstream
[Python API guide](https://doronz88.github.io/pymobiledevice3/guides/python-api/)
and [tunnel guide](https://doronz88.github.io/pymobiledevice3/guides/ios17-tunnels/).

Run hardware-free checks with:

```sh
python -m unittest discover -s tests -v
```

The 15 hardware-free checks passed locally, including coordinate boundaries,
platform gates, hidden error details, failed Set/Clear cleanup, cancelled cleanup,
partial service handshakes, failed tunnel startup, and supervisor EOF handling.

**On-device testing is pending**: macOS detection, the administrator dialog, USB
tunnel establishment, iOS 26.2 image mounting, Maps Set/Clear behavior, and tunnel
termination need your Mac/iPhone test. After stage 1 passes, stage 2 adds FastAPI
bound only to 127.0.0.1, stage 3 adds the Leaflet/OSM single-page UI, and stage 4
adds waypoint playback with speed and Start/Pause/Stop.
