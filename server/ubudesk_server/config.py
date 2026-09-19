"""Configuration and state directory handling.

State lives in ``~/.config/ubudesk/`` (override with ``UBUDESK_STATE_DIR`` for tests):

    config.toml   - user configuration
    devices.json  - paired devices (token *hashes* only)
    cert.pem      - self-signed TLS certificate
    key.pem       - private key (0600)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def state_dir() -> Path:
    override = os.environ.get("UBUDESK_STATE_DIR")
    if override:
        d = Path(override)
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        d = Path(xdg) / "ubudesk"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


@dataclass
class Config:
    port: int = 7777
    bind: str = "0.0.0.0"
    source: str = "portal"  # portal | test
    mode: str = "extend"  # extend | mirror
    encoder: str = "auto"  # auto | x264 | va | nvenc
    tls: bool = True
    log_level: str = "info"
    server_name: str = ""
    # xdg-desktop-portal restore token, saved after a successful session
    restore_token: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls) -> Config:
        path = state_dir() / "config.toml"
        cfg = cls()
        if path.exists():
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            for key, value in data.items():
                if hasattr(cfg, key) and key != "extra":
                    setattr(cfg, key, value)
                else:
                    cfg.extra[key] = value
        if not cfg.server_name:
            import socket

            cfg.server_name = socket.gethostname()
        return cfg

    def save(self) -> None:
        path = state_dir() / "config.toml"
        lines = ["# UbuDesk server configuration\n"]
        for key in (
            "port",
            "bind",
            "source",
            "mode",
            "encoder",
            "tls",
            "log_level",
            "server_name",
            "restore_token",
        ):
            value = getattr(self, key)
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}\n")
            elif isinstance(value, int):
                lines.append(f"{key} = {value}\n")
            else:
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key} = "{escaped}"\n')
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(lines))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
