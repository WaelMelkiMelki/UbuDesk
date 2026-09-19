"""Pairing (PIN) and token authentication.

Security properties:
- Tokens are 32 random bytes (base64url on the wire); only SHA-256 hashes are stored.
- PINs are 6 digits, single use, expire after 10 minutes, max 5 wrong attempts
  then a 60 s lockout.
- All comparisons are constant-time (hmac.compare_digest).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PIN_TTL_S = 600.0
PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT_S = 60.0


def _hash_token(token_bytes: bytes) -> str:
    return hashlib.sha256(token_bytes).hexdigest()


def generate_token() -> tuple[str, str]:
    """Return (token_b64, token_hash_hex)."""
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).decode().rstrip("="), _hash_token(raw)


def _decode_token(token_b64: str) -> bytes | None:
    try:
        pad = "=" * (-len(token_b64) % 4)
        return base64.urlsafe_b64decode(token_b64 + pad)
    except Exception:
        return None


@dataclass
class PairedDevice:
    client_id: str
    name: str
    token_hash: str
    paired_at: float
    last_seen: float


class DeviceStore:
    """Persisted paired-device registry (devices.json, 0600)."""

    def __init__(self, path: Path):
        self._path = path
        self._devices: dict[str, PairedDevice] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("could not read %s; starting with no paired devices", self._path)
            return
        for cid, entry in data.items():
            self._devices[cid] = PairedDevice(
                client_id=cid,
                name=entry.get("name", "?"),
                token_hash=entry.get("token_hash", ""),
                paired_at=entry.get("paired_at", 0.0),
                last_seen=entry.get("last_seen", 0.0),
            )

    def _save(self) -> None:
        data = {
            d.client_id: {
                "name": d.name,
                "token_hash": d.token_hash,
                "paired_at": d.paired_at,
                "last_seen": d.last_seen,
            }
            for d in self._devices.values()
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)

    def add(self, client_id: str, name: str, token_hash: str) -> None:
        now = time.time()
        self._devices[client_id] = PairedDevice(client_id, name, token_hash, now, now)
        self._save()

    def remove(self, client_id: str) -> bool:
        if client_id in self._devices:
            del self._devices[client_id]
            self._save()
            return True
        return False

    def list(self) -> list[PairedDevice]:
        return sorted(self._devices.values(), key=lambda d: d.paired_at)

    def __len__(self) -> int:
        return len(self._devices)

    def verify_token(self, client_id: str, token_b64: str) -> bool:
        """Constant-time token check for a client id."""
        raw = _decode_token(token_b64)
        if raw is None:
            return False
        presented = _hash_token(raw)
        device = self._devices.get(client_id)
        # Always run a comparison so timing does not reveal whether the id exists.
        expected = device.token_hash if device else "0" * 64
        ok = hmac.compare_digest(presented, expected) and device is not None
        if ok and device is not None:
            device.last_seen = time.time()
            self._save()
        return ok


class PinManager:
    """Single-use pairing PIN with expiry, attempt limit and lockout."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._pin: str | None = None
        self._issued_at = 0.0
        self._attempts = 0
        self._locked_until = 0.0

    def issue(self) -> str:
        self._pin = f"{secrets.randbelow(1_000_000):06d}"
        self._issued_at = self._clock()
        self._attempts = 0
        return self._pin

    @property
    def active(self) -> bool:
        return self._pin is not None and (self._clock() - self._issued_at) <= PIN_TTL_S

    @property
    def locked(self) -> bool:
        return self._clock() < self._locked_until

    def lockout_remaining(self) -> int:
        return max(0, int(self._locked_until - self._clock()) + 1) if self.locked else 0

    def verify(self, pin: str) -> str:
        """Return 'ok' | 'bad_pin' | 'pin_expired' | 'locked'. PIN is consumed on success."""
        if self.locked:
            return "locked"
        if self._pin is None or (self._clock() - self._issued_at) > PIN_TTL_S:
            return "pin_expired"
        if hmac.compare_digest(pin, self._pin):
            self._pin = None  # single use
            return "ok"
        self._attempts += 1
        if self._attempts >= PIN_MAX_ATTEMPTS:
            self._locked_until = self._clock() + PIN_LOCKOUT_S
            self._pin = None
            log.warning("too many wrong PIN attempts; pairing locked for %ss", PIN_LOCKOUT_S)
            return "locked"
        return "bad_pin"
