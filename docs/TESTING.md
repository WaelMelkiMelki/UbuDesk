# Testing

## Automated (runs headless in CI — no display, no GPU, no device)

### Server (`server-ci.yml`, ubuntu-24.04)

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy ubudesk_server
.venv/bin/python -m pytest tests -v
```

What the suite covers:

| Area | Tests |
|---|---|
| Protocol framing | golden vectors round-trip, oversize/truncated/bad-JSON rejection (`test_protocol.py`) |
| Auth | PIN success/single-use/expiry/lockout, token hash round-trip, revoke (`test_auth.py`) |
| Security invariant | `start`/input **before** auth closes the connection and never reaches capture/input (`test_session_security.py`) |
| TLS | cert generation (0600 key), fingerprint pinning, mismatch rejection (`test_tls.py`) |
| Test video source | first AU = IDR+SPS/PPS, IDR-on-request < 3 s, pts monotonic, ~30 fps (`test_test_source.py`) |
| Integration | real server + `tools/test_client.py` over loopback: ≥30 frames/3 s, first frame keyframe with SPS/PPS, IDR within 1 s of request, touch reaches `FakeInput`, plain TCP **and** TLS, token reconnect + revoke, clean port release (`test_integration.py`) |
| Backpressure | queue flood drops deltas, keyframe survives, IDR requested (`test_backpressure.py`) |
| Config/doctor | toml round-trip, doctor runs headless, `--json` shape incl. support matrix (`test_config_doctor.py`) |
| X11 ladder | xrandr query parsing, candidate-output picking, cvt parsing + fallback modeline, full ladder happy/refusal paths, stale cleanup and teardown with a scripted fake xrandr (`test_x11.py`) |
| Encoders | fragment/tail construction, property application by introspection with a fake element (`test_encoders.py`) |

Plus a CLI smoke test in CI: `ubudesk serve --source test` + `tools/test_client.py`
end to end as real processes, dumping a decodable `.h264` file.

### Android (`android-ci.yml`, ubuntu-latest)

```bash
cd android
./gradlew lintDebug testDebugUnitTest assembleDebug
```

JVM unit tests (no emulator): `ProtocolVectorTest` (same golden vectors as
Python), `TouchMapperTest` (letterbox math), `PinnedTlsTest`,
`SettingsTest` (settings → `start` message, server-list persistence).

## Manual verification

Everything that needs real hardware is in **[MANUAL_TEST.md](MANUAL_TEST.md)** —
that file is the honest list of what CI *cannot* prove.
