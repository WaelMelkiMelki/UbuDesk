# Testing

## Automated (runs headless in CI — no display, no GPU, no device)

### Server (`server-ci.yml`, Ubuntu 22.04/Python 3.10 and Ubuntu 24.04/Python 3.12)

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
| Session lifecycle | delayed PIN entry, bounded authentication, pings during pending capture, cancellation/late-result disposal, failed starts, restart isolation, held-input release, revocation during startup (`test_session_lifecycle.py`) |
| Portal requests | D-Bus argument construction, cancellation before/during requests, request dismissal, timeout cleanup, unique handles, native stop failures (fake GI, **not** real-desktop proof; `test_portal_requests.py`) |
| Backpressure | queue flood drops deltas, keyframe survives, IDR requested (`test_backpressure.py`) |
| Config/doctor | toml round-trip, doctor runs headless, `--json` shape incl. support matrix (`test_config_doctor.py`) |
| X11 ladder | xrandr query parsing, candidate-output picking, cvt parsing + fallback modeline, full ladder happy/refusal paths, stale cleanup and teardown with a scripted fake xrandr (`test_x11.py`) |
| Encoders | fragment/tail construction, property application by introspection with a fake element (`test_encoders.py`) |

Plus a CLI smoke test in CI: `ubudesk serve --source test` + `tools/test_client.py`
end to end as real processes, dumping a decodable `.h264` file.

Revocation integration tests invoke the real `ubudesk devices --revoke` CLI
in a **separate process**, then check active-stream termination and rejection
of the revoked token on reconnect over both TCP and TLS. Device-store tests
also cover stale instances, concurrent updates, and malformed/deleted state.

### Android (`android-ci.yml`, Ubuntu 24.04, Java 17, SDK 36)

```bash
cd android
./gradlew lintDebug testDebugUnitTest assembleDebug
```

JVM unit tests (no emulator): `ProtocolVectorTest` (same golden vectors as
Python), `TouchMapperTest` (letterbox math), `PinnedTlsTest`,
`SettingsTest` (settings → `start` message, server-list persistence), and
`UbuDeskClientTest` (real loopback sockets: no pre-auth heartbeats, delayed PIN
entry, bounded pairing, post-auth idle timeouts, capture-permission keepalive,
EOF/auth failure/TLS-handshake cancellation cleanup, concurrent disconnects).

The installed workflows are in `.github/workflows/`. Keep them in sync with
`ci/workflows/` using `./scripts/enable-ci.sh`; CI also runs its `--check` mode.
Both workflows support pushes, pull requests, and manual dispatch. The separate
release publisher is not activated by this setup.

A **successful** Android run publishes `ubudesk-debug-<commit>` for 14 days:
`app-debug.apk`, a matching `ubudesk-test-source.tar.gz`, `SHA256SUMS`,
`build-info.txt`, and the quick hardware-test guide. APK signatures are verified
before upload. Test/lint reports are uploaded even on failure when available;
the server matrix uploads its JUnit results as well.

Download the artifact from the run's **Artifacts** section, or use:

```bash
gh run download RUN_ID --repo WaelMelkiMelki/UbuDesk --pattern 'ubudesk-debug-*' --dir artifacts
```

Use the matching server source, not an older `main` checkout, for device tests.
See [QUICK_TEST.md](QUICK_TEST.md) for install, pairing, and first-stream checks.

## Manual verification

Everything that needs real hardware is in **[MANUAL_TEST.md](MANUAL_TEST.md)** —
that file is the honest list of what CI *cannot* prove.
