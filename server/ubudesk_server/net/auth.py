"""Pairing (PIN) and token authentication.

Security properties:
- Tokens are 32 random bytes (base64url on the wire); only SHA-256 hashes are stored.
- PINs are 6 digits, single use, expire after 10 minutes, max 5 wrong attempts
  then a 60 s lockout.
- All comparisons are constant-time (hmac.compare_digest).
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
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


def token_fingerprint(token_b64: str) -> str | None:
    """Hash a wire token without retaining its bearer credential in a session."""
    raw = _decode_token(token_b64)
    return _hash_token(raw) if raw is not None and len(raw) == 32 else None


class DeviceStore:
    """Process-safe paired-device registry (devices.json, 0600).

    Every operation reloads under a stable sidecar file lock. Locking the JSON
    file itself is not sufficient: atomic replacement changes its inode. This
    prevents a running server's last_seen update from undoing a CLI revocation
    or overwriting a device added by another process.
    """

    def __init__(self, path: Path):
        self._path = path
        self._devices: dict[str, PairedDevice] = {}

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        fd = os.open(self._path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self._load()
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _load(self) -> None:
        # Fail closed on deletion, unreadable data, or malformed JSON; never
        # keep previously trusted entries after an unsuccessful reload.
        self._devices = {}
        try:
            data = json.loads(self._path.read_text())
            if not isinstance(data, dict):
                raise ValueError("registry must be an object")
            devices = {}
            for cid, entry in data.items():
                if not isinstance(entry, dict):
                    raise ValueError("invalid device entry")
                token_hash = entry.get("token_hash", "")
                if (
                    not isinstance(token_hash, str)
                    or len(token_hash) != 64
                    or any(ch not in "0123456789abcdef" for ch in token_hash)
                ):
                    raise ValueError("invalid token hash")
                devices[cid] = PairedDevice(
                    client_id=cid,
                    name=str(entry.get("name", "?")),
                    token_hash=token_hash,
                    paired_at=float(entry.get("paired_at", 0.0)),
                    last_seen=float(entry.get("last_seen", 0.0)),
                )
            self._devices = devices
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            log.warning("could not read %s; treating all devices as unpaired", self._path)

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
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                os.fchmod(fh.fileno(), 0o600)
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self._path)
        finally:
            tmp.unlink(missing_ok=True)

    def add(self, client_id: str, name: str, token_hash: str) -> None:
        with self._transaction():
            now = time.time()
            self._devices[client_id] = PairedDevice(client_id, name, token_hash, now, now)
            self._save()

    def remove(self, client_id: str) -> bool:
        with self._transaction():
            if client_id in self._devices:
                del self._devices[client_id]
                self._save()
                return True
            return False

    def list(self) -> list[PairedDevice]:
        with self._transaction():
            return sorted(self._devices.values(), key=lambda d: d.paired_at)

    def __len__(self) -> int:
        with self._transaction():
            return len(self._devices)

    def _matches(self, client_id: str, presented: str) -> bool:
        device = self._devices.get(client_id)
        # Always compare, including when the id does not exist.
        expected = device.token_hash if device else "0" * 64
        return hmac.compare_digest(presented, expected) and device is not None

    def verify_token(self, client_id: str, token_b64: str) -> bool:
        presented = token_fingerprint(token_b64)
        if presented is None:
            return False
        with self._transaction():
            if not self._matches(client_id, presented):
                return False
            self._devices[client_id].last_seen = time.time()
            self._save()
            return True

    def is_authorized(self, client_id: str, token_hash: str) -> bool:
        """Read-only recheck for active sessions, including re-pair/token rotation."""
        with self._transaction():
            return self._matches(client_id, token_hash)


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
