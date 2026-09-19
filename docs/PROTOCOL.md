# UbuDesk wire protocol — v1

Transport: TCP, wrapped in **TLS 1.2+** by default (`--no-tls` exists for
tests/dev only). Default port **7777**. `TCP_NODELAY` on both sides.

## 1. Framing

```
| type: u8 | length: u32 big-endian | payload: `length` bytes |
```

- `0x01` — **CONTROL**: UTF-8 JSON object with field `"t"`.
- `0x02` — **VIDEO** (server → client only):

  ```
  | pts_us: u64 BE | flags: u8 | data |
  ```

  - `flags` bit0 = keyframe (IDR), bit1 = contains SPS/PPS.
  - `data` = one H.264 **access unit, Annex-B** byte stream (start codes).
    SPS/PPS are **prepended to every IDR** (`h264parse config-interval=-1`
    on GStreamer; `repeat-headers=1` on x264).

Rules:

- Max payload **8 MiB**; a larger announced length ⇒ close the connection.
- Unknown frame *types*: read and ignore the payload.
- Unknown JSON `"t"` values: ignore the message.
- A client must never send VIDEO frames; the server closes if it does.

## 2. Handshake

```
1. C→S {"t":"hello","proto":1,"client_id":"<uuid>","name":"Pixel Tablet",
        "app_version":"0.1.0",
        "screen":{"w":2560,"h":1600,"dpi":320,"refresh":60},
        "codecs":["h264"]}
2. S→C {"t":"auth_required","server_id":"<uuid>","server_name":"my-pc",
        "methods":["token","pin"]}
3. C→S {"t":"auth","method":"token","token":"<b64url>"}
      | {"t":"auth","method":"pin","pin":"123456"}
4. S→C {"t":"auth_ok","token":"<b64url, present only after PIN pairing>"}
      | {"t":"auth_fail","reason":"bad_pin|pin_expired|locked|unknown_token",
         "retry_after_s":60}          → then the server closes
5. C→S {"t":"start","width":1920,"height":1200,"fps":60,
        "bitrate_kbps":15000,"mode":"extend","touch_mode":"touch"}
6. S→C {"t":"started","width":1920,"height":1200,"fps":60,
        "codec":"h264","encoder":"x264enc"}      → followed by VIDEO frames
      | {"t":"error","code":"capture_failed|no_virtual_monitor|bad_request",
         "message":"..."}
```

**Nothing except `hello` and `auth` is accepted before `auth_ok`.** The
server closes the connection on any violation. `mode` is `extend` (virtual
monitor) or `mirror` (existing monitor).

`start` may be sent again while streaming to renegotiate (resolution/fps
change): the server stops the old stream, starts a new one, and replies with
a fresh `started`.

## 3. Runtime messages

Client → server. Coordinates are **normalized 0..1 relative to the video
area** (the client accounts for letterboxing).

```json
{"t":"touch","a":"down|move|up|cancel","id":0,"x":0.51,"y":0.25}
{"t":"mouse","a":"move|down|up","b":"left|right|middle","x":0.4,"y":0.7}
{"t":"scroll","dx":0.0,"dy":1.0}
{"t":"key","code":30,"down":true}
{"t":"text","s":"héllo 😀"}
{"t":"ping","seq":1,"ts":123456789}
{"t":"idr"}
{"t":"bitrate","kbps":8000}
{"t":"stats","fps":58,"decode_ms":4.1,"dropped":0}
{"t":"bye"}
```

- `touch.id` is the pointer/slot id, 0..9.
- `scroll`: discrete steps; **+dy = scroll down**, +dx = scroll right.
- `key.code` is a **Linux evdev keycode** (Android `KeyEvent.scanCode`).
- `text` is typed via keysyms (Unicode keysym = `0x01000000 + codepoint`).
- `idr` asks the server to force a keyframe as soon as possible.
- `bitrate` changes the encoder bitrate live.

Server → client:

```json
{"t":"pong","seq":1,"ts":123456789}
{"t":"error","code":"...","message":"..."}
{"t":"bye"}
```

**Idle rule:** the client pings every 2 s; either side may close the
connection after 6 s of silence.

## 4. Video stream invariants

- The **first frame after `started` is an IDR** with in-band SPS/PPS.
- Every IDR access unit carries SPS/PPS before the IDR slice.
- No B-frames; PTS is monotonically increasing microseconds.
- A keyframe is produced at least every ~2 s (`key-int-max = 2*fps`) and on
  every `idr` request.
- Backpressure: when the send queue backs up, the server drops delta frames,
  forces an IDR, and resumes from it (the client may briefly see a frame skip
  but never grows latency).

## 5. Golden vectors

`protocol/vectors/` contains `.bin` (exact wire bytes) + `.json` (expected
decode) pairs for every control message, keyframe/delta/large-pts video
frames, and malformed frames (oversized, truncated, bad JSON).
`protocol/vectors/generate.py` regenerates them; server CI fails if they
drift. **Both** the Python tests (`server/tests/test_protocol.py`) and the
Kotlin tests (`android/app/src/test/.../ProtocolVectorTest.kt`) run against
the same files.

## 6. Pairing & security

- PIN: 6 digits, printed by `ubudesk serve --pair` (or automatically when no
  device is paired). Single use, expires after **10 minutes**, max **5**
  wrong attempts then a **60 s** lockout.
- Token: 32 random bytes, base64url on the wire; the server stores only the
  SHA-256 hash (`~/.config/ubudesk/devices.json`).
- TLS: self-signed EC P-256 cert generated on first run. The Android client
  pins the **SHA-256 fingerprint of the leaf certificate** after the first
  successful pairing (trust-on-first-use). Both sides display the first
  8 hex chars ("security code") so the user can compare during pairing.
  Residual risk: an active MITM **during the very first pairing** could
  intercept; compare the security code to rule that out.
