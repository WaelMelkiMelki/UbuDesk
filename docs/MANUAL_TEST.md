# Manual test checklist

CI proves the protocol, auth, backpressure, and the headless video pipeline.
The items below need a real Ubuntu desktop and/or a real Android device.
Please run them top to bottom and report failures with the bug-report
template (`.github/ISSUE_TEMPLATE/bug_report.md`).

## Verified on real hardware?

| Feature | Automated proof | Real-hardware status |
|---|---|---|
| Protocol framing + handshake | ✅ CI (both languages, shared vectors) | inherits from CI |
| PIN pairing / tokens / lockout | ✅ CI | needs a quick phone check (M4-1) |
| TLS + fingerprint pinning | ✅ CI (server side + client TrustManager logic) | needs phone check (M4-2) |
| Test-pattern streaming end to end | ✅ CI (server + fake client) | **NO — needs M1-1** |
| MediaCodec decode on a phone | ❌ cannot run in CI | **NO — needs M1-1** |
| Portal mirror capture | ❌ needs GNOME session | **NO — needs M2-1** |
| Portal VIRTUAL monitor (extend) | ❌ needs GNOME session | **NO — needs M3-1** |
| Portal input injection | ❌ needs GNOME session | **NO — needs M5-x** |
| uinput fallback input | ❌ needs /dev/uinput | **NO — needs M5-4** |
| Hardware encoders (VA/NVENC) | ❌ needs GPU | **NO — needs M6-1** |
| mDNS discovery | ⚠️ advertise starts in CI, browse untested | **NO — needs M4-3** |
| USB mode | ❌ needs device | **NO — needs M4-4** |

## Setup (once)

```bash
git clone <repo> && cd <repo>/server
./scripts/install-deps.sh
source .venv/bin/activate
ubudesk doctor            # everything should be PASS/WARN; copy output if not
```

Phone: install `app-debug.apk` from the GitHub Actions artifact (or build
with `cd android && ./gradlew assembleDebug`), allow unknown sources.

---

### M1-1 — Test-pattern stream to the phone (no desktop capture involved)

```bash
ubudesk serve --source test --pair
```

Phone: connect to `PC_IP:7777` (or pick the discovered server), enter the PIN.
**Expected:** a moving white ball on a gray background, smooth ≥30 fps, and a
flickering binary counter strip along the top edge.
**If it fails:** check `sudo ufw allow 7777/tcp`; try Settings → enable
"Compatibility decode"; capture `adb logcat -s UbuDesk` and server
`--log-level debug` output.

### M2-1 — Mirror the real desktop

```bash
ubudesk serve --mode mirror       # or set mode=mirror in the app settings
```

Phone connects → GNOME shows a screen-share dialog → pick a monitor.
**Expected:** your desktop appears on the phone; dragging windows is visible
with < ~150 ms lag on 5 GHz Wi-Fi.
**If it fails:** run `server/.venv/bin/python server/scripts/portal_probe.py
--capture --mode mirror` and attach the output.

### M2-2 — Portal probe (paste results back)

```bash
server/.venv/bin/python server/scripts/portal_probe.py --capture
```

**Expected:** `VIRTUAL monitor support: YES` on Ubuntu 24.04 GNOME Wayland,
and `Captured 100 buffers … OK`.
**If NO:** extend mode is unavailable on this machine; mirror still works.
Note the printed portal versions in your report.

### M3-1 — Extend mode (virtual monitor)

App Settings → mode = extend → connect.
**Expected:** a new display appears in GNOME Settings → Displays; windows can
be dragged onto it and show on the phone. Disconnecting removes the display
and the windows return.
**If it fails with `no_virtual_monitor`:** expected on portals without
VIRTUAL support — see TROUBLESHOOTING.md.

### M3-2 — Cleanup on hard kill

While streaming in extend mode: `kill -9 $(pgrep -f "ubudesk serve")`.
**Expected:** the virtual monitor disappears within a few seconds (the portal
session dies with the process).

### M4-1 — Pairing edge cases

- Wrong PIN 5 times → app shows "locked … 60 s"; server log shows the lockout.
- Reconnect after pairing → **no PIN asked** (token reused).
- `ubudesk devices` lists the phone; `ubudesk devices --revoke <id>` then
  reconnect → app falls back to pairing.

### M4-2 — Pinning

Pair once, then on the PC: `rm ~/.config/ubudesk/cert.pem ~/.config/ubudesk/key.pem`
and restart the server (new cert).
**Expected:** the app **refuses** to connect (fingerprint mismatch message).
"Forget" the server in the app, reconnect, compare the security code shown on
both sides, pair again.

### M4-3 — Discovery

With server running, open the app on the same Wi-Fi.
**Expected:** the server appears under "Discovered on this network" within
~5 s. If not, manual IP must still work (mDNS may be blocked by the AP).

### M4-4 — USB mode

Wi-Fi **off** on the phone, USB debugging on, cable connected:

```bash
ubudesk usb          # runs adb reverse tcp:7777 tcp:7777
```

App → tap **USB**. **Expected:** stream works over the cable.

### M5-1..5 — Input (on the mirrored/extended screen)

1. Tap a desktop icon → it activates at the right position (also near all
   4 corners — checks the coordinate mapping).
2. Drag a window by its title bar → follows the finger.
3. Mouse mode + two-finger drag → scrolls a web page in the natural direction.
4. Mouse mode + two-finger tap → right-click menu appears.
5. Hardware keyboard (if available): typing, Enter, Backspace; soft-keyboard
   text incl. non-ASCII (`héllo 😀`).

### M6-1 — Encoder selection

`ubudesk serve --log-level debug` on a machine with Intel/AMD (VA) or NVIDIA:
**Expected:** log line `using encoder: vah264enc` (or `nvh264enc`), and
`--encoder x264` still works as an override.

### M6-2 — Stability

- 10-minute desktop-drag session: latency must not creep up (backpressure
  drops instead).
- Leave the desktop completely static for 60 s: the stream must **not**
  freeze or disconnect (keepalive frames).
- Toggle Wi-Fi off/on: app shows the disconnect and reconnects cleanly from
  the Connect screen.

### M7-1 — systemd unit

```bash
server/packaging/install.sh
systemctl --user start ubudesk && systemctl --user status ubudesk
```

**Expected:** service is active; a phone can connect. `systemctl --user stop
ubudesk` stops it cleanly.
