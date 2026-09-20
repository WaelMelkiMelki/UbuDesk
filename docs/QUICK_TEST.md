# First Ubuntu + Android test

A debug APK passing CI is **not** proof that desktop capture or MediaCodec
works on your hardware. This short test checks the basics before trying extend,
USB, or input. Use a trusted local network; do not forward port 7777 to the internet.

## 1. Download the matching test build

Open a **successful `android-ci` run** on GitHub for the branch/commit you want
to test. Under **Artifacts**, download and extract `ubudesk-debug-<commit>`.
The artifact is retained for 14 days and contains:

- `app-debug.apk` — Android 8.0/API 26 or newer; debug-signed, not a production release.
- `ubudesk-test-source.tar.gz` — server and source from the exact same commit.
- `SHA256SUMS` — checksums of the APK and source archive.
- `build-info.txt` — commit, branch/ref, and CI run URL.
- `README-testing.md` — this guide.

On Ubuntu, in the extracted artifact directory:

```bash
sha256sum -c SHA256SUMS
```

Both files must report `OK`. Extract the source into a **new test directory**,
not over an existing checkout with your own work:

```bash
mkdir ubudesk-device-test
tar -xzf ubudesk-test-source.tar.gz -C ubudesk-device-test
cd ubudesk-device-test/UbuDesk/server
./scripts/install-deps.sh
.venv/bin/ubudesk doctor
```

The installer needs sudo and network access for Ubuntu dependencies. The easiest
first target is Ubuntu 24.04 with GNOME. Warnings about virtual-monitor support
or hardware encoders need not prevent the synthetic test-pattern check.
If installation or `doctor` reports a capture dependency error, keep its output.

## 2. Install the APK on your phone/tablet

Transfer `app-debug.apk` to the device and open it, allowing installation from
that file manager/browser when Android asks. Alternatively, with Android's USB
debugging enabled and the computer authorized:

```bash
adb devices
adb install -r /path/to/app-debug.apk
```

CI runners generate development signing keys. If Android reports
`INSTALL_FAILED_UPDATE_INCOMPATIBLE`, a previous debug APK may have a different
key. **Uninstalling UbuDesk clears its saved pairings/settings**; only after
accepting that data loss, uninstall the old app and install this build again.
Do not disable Android's signature checks.

## 3. Check the test-pattern stream first

Put the phone and PC on the same trusted Wi-Fi/LAN (not an isolated guest
network). In the server directory, run:

```bash
.venv/bin/ubudesk serve --source test --bind 0.0.0.0 --pair
```

Find the PC's LAN IPv4 address with `hostname -I`. On the phone:

1. Select the discovered PC, or enter its LAN address and port **7777**.
2. Compare the displayed security code with the server console before entering
   the six-digit PIN. Do not share the PIN or saved token in bug reports.
3. For the timing regression, wait **15–30 seconds** on the PIN screen before
   submitting (the pairing window is two minutes).
4. Expect a moving ball/counter pattern. Start with a modest resolution and
   30 FPS if the PC is slow.
5. Disconnect and reconnect. Repeat a few times; the app must not jump back to
   an old connection or leave the server stuck.

This path tests TLS, pairing, video transport, and Android decoding without
needing a real desktop-capture portal. If discovery fails, try the manual IP.
If the connection itself fails, check host firewall rules for TCP 7777 and
Wi-Fi client isolation; allow only the trusted LAN/device, not internet access.

## 4. Check real desktop mirroring

Stop the test server with **Ctrl+C**. In the Android app, explicitly set
**Settings → mode = mirror**; the app's `start` request selects the capture mode.
Then run on the PC:

```bash
.venv/bin/ubudesk serve --source auto --mode mirror --bind 0.0.0.0 --pair
```

Reconnect. On GNOME Wayland, select the monitor in the desktop permission
dialog. Leave the dialog open for **15 seconds** before approving: the phone
must stay connected. Verify that moving a window is visible on the phone.

Repeat once, cancelling/disconnecting on the phone while the desktop dialog
is open. The request should close, and a late approval must not reopen the old
stream. On X11, permission-dialog checks do not apply; record the session type.

Once mirror is stable, open `UbuDesk/docs/MANUAL_TEST.md` in the source
archive for revocation, input, extend, hardware encoders, and USB. Do not treat
successful video decoding as proof that touch coordinates or extend mode work.

## 5. Report what actually happened

Record the following (leave untested items marked **not tested**):

| Item | Result |
|---|---|
| Build commit from `build-info.txt` | |
| Ubuntu version, desktop, Wayland/X11 | |
| Phone/tablet model and Android version | |
| Test pattern displays and animates | |
| PIN entry after a 15–30 second delay | |
| Disconnect/reconnect | |
| Real desktop mirror | |
| Permission dialog wait/cancel (Wayland) | |
| Input / extend / USB | not tested |

For failures, save the error text and `ubudesk doctor` output. Debug logs:

```bash
.venv/bin/ubudesk serve --log-level debug --source test --pair
adb logcat -s UbuDesk
```

Redact PINs, bearer tokens, and other personal information before sharing logs.
Only test locally: CI does not have access to your desktop session or phone.
