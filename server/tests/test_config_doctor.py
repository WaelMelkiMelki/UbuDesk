"""Config round-trip and doctor output tests."""

import json

from ubudesk_server.config import Config, state_dir
from ubudesk_server.doctor import format_report, run_doctor


def test_config_roundtrip():
    cfg = Config.load()
    cfg.port = 7878
    cfg.tls = False
    cfg.restore_token = 'tok"with\\quotes'
    cfg.save()
    cfg2 = Config.load()
    assert cfg2.port == 7878
    assert cfg2.tls is False
    assert cfg2.restore_token == 'tok"with\\quotes'


def test_config_defaults():
    cfg = Config.load()
    assert cfg.port == 7777
    assert cfg.source == "portal"
    assert cfg.mode == "extend"
    assert cfg.tls is True
    assert cfg.server_name


def test_doctor_runs_headless():
    report = run_doctor()
    names = {c.name for c in report.checks}
    assert "os" in names
    assert "session" in names
    # in a headless container the session check fails - that's correct behavior
    text = format_report(report)
    assert "virtual monitor:" in text


def test_doctor_json():
    report = run_doctor()
    out = json.loads(format_report(report, as_json=True))
    assert "checks" in out and "verdict" in out and "ok" in out
    for check in out["checks"]:
        assert check["status"] in ("PASS", "WARN", "FAIL")


def test_state_dir_permissions():
    d = state_dir()
    assert (d.stat().st_mode & 0o777) == 0o700
