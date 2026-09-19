# Troubleshooting

Always start with:

```bash
cd server && source .venv/bin/activate
ubudesk doctor          # add --json when filing a bug
```

Every FAIL/WARN prints the exact fix command. Common cases:

## "no_virtual_monitor" — extend mode unavailable

Your xdg-desktop-portal does not advertise VIRTUAL sources
(`AvailableSourceTypes` bit 4 unset). Options, best first:

1. **Update**: Ubuntu 24.04+ with GNOME on Wayland supports it via
   `xdg-desktop-portal-gnome`. `sudo apt install xdg-desktop-portal-gnome`,
   then log out/in.
2. **Check the session type**: `echo $XDG_SESSION_TYPE` must print `wayland`.
   On the login screen pick "Ubuntu" (Wayland), not "Ubuntu on Xorg".
3. **Use mirror mode** (app Settings → mode → mirror). Works everywhere.
4. X11 virtual-output support (xrandr/EVDI) is planned (milestone M8) but not
   implemented yet.

Confirm with:

```bash
server/.venv/bin/python server/scripts/portal_probe.py
```

## The GNOME permission dialog appears on every connection

The portal should return a `restore_token` that UbuDesk stores in
`~/.config/ubudesk/config.toml` and replays. Some GNOME versions do not
persist tokens for VIRTUAL sources — if the dialog keeps coming back, that is
a GNOME limitation; it is a single click per connection.

## Video never appears on the phone (audio of clicks works, screen black)

1. App Settings → enable **Compatibility decode** (disables the low-latency
   MediaCodec hints; some SoCs need this).
2. Tap **Refresh** in the stream overlay (forces an IDR).
3. Lower the resolution preset to 1280x800.
4. Capture `adb logcat -s UbuDesk` and file a bug.

## "certificate fingerprint mismatch"

The server's TLS key changed (reinstall, deleted `~/.config/ubudesk`).
If that was you: "Forget" the server in the app and pair again, comparing
the 8-character security code shown on both sides. If it was NOT you, stop —
that is exactly what a man-in-the-middle looks like.

## Phone doesn't discover the server

- Same L2 network? Guest Wi-Fi and AP isolation block mDNS.
- Manual `IP:7777` always works; find the IP with `ip -4 addr`.
- Firewall: `sudo ufw allow 7777/tcp`.

## USB mode fails

```bash
ubudesk usb
```

prints the exact problem: adb missing (`sudo apt install android-tools-adb`),
device unauthorized (accept the RSA prompt on the phone), or no device.
Then connect the app to `127.0.0.1:7777` (the USB button does this).

## Choppy / laggy stream

- Prefer 5 GHz Wi-Fi or USB mode; 2.4 GHz is usually the culprit.
- Lower bitrate (Settings) or resolution.
- Check the server log for `send backlog; dropping until next keyframe` —
  frequent occurrences mean the network cannot carry the bitrate.
- `ubudesk serve --log-level debug` shows encoder choice; software x264 on an
  old CPU may cap at ~30 fps at 1920x1200 — that is expected.

## Static screen freezes the stream

Should not happen (`keepalive-time=100` re-sends frames). If it does, run
with `--log-level debug` and file a bug with the last 50 log lines.

## Input does nothing (view works)

- The portal session must include input devices: reconnect and accept the
  full "remote control" dialog, not just screen share.
- `ubudesk doctor` → `remote_desktop` line tells whether the RemoteDesktop
  portal is reachable.
- X11 fallback needs `/dev/uinput` access:
  `sudo cp server/packaging/99-ubudesk.rules /etc/udev/rules.d/ &&
  sudo udevadm control --reload && sudo usermod -aG input $USER`, re-login.
