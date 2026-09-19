"""Auth unit tests: PIN lifecycle, tokens, device store."""

from pathlib import Path

from ubudesk_server.net import auth


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def test_pin_success_is_single_use():
    clock = FakeClock()
    pm = auth.PinManager(clock=clock)
    pin = pm.issue()
    assert len(pin) == 6 and pin.isdigit()
    assert pm.verify(pin) == "ok"
    # second use fails
    assert pm.verify(pin) == "pin_expired"


def test_pin_expiry():
    clock = FakeClock()
    pm = auth.PinManager(clock=clock)
    pin = pm.issue()
    clock.advance(auth.PIN_TTL_S + 1)
    assert pm.verify(pin) == "pin_expired"


def test_pin_lockout_and_recovery():
    clock = FakeClock()
    pm = auth.PinManager(clock=clock)
    pin = pm.issue()
    wrong = "000000" if pin != "000000" else "111111"
    for _ in range(auth.PIN_MAX_ATTEMPTS - 1):
        assert pm.verify(wrong) == "bad_pin"
    assert pm.verify(wrong) == "locked"
    # even the right PIN is refused while locked
    assert pm.verify(pin) == "locked"
    assert pm.lockout_remaining() > 0
    clock.advance(auth.PIN_LOCKOUT_S + 1)
    # after lockout the old PIN is gone; a new one must be issued
    assert pm.verify(pin) == "pin_expired"
    pin2 = pm.issue()
    assert pm.verify(pin2) == "ok"


def test_token_roundtrip(tmp_path: Path):
    store = auth.DeviceStore(tmp_path / "devices.json")
    token_b64, token_hash = auth.generate_token()
    assert token_b64 not in token_hash  # hash stored, not the token
    store.add("client-1", "Pixel", token_hash)

    assert store.verify_token("client-1", token_b64)
    assert not store.verify_token("client-1", token_b64 + "x")
    assert not store.verify_token("client-2", token_b64)
    assert not store.verify_token("client-1", "!!!not-base64!!!")

    # reload from disk
    store2 = auth.DeviceStore(tmp_path / "devices.json")
    assert store2.verify_token("client-1", token_b64)
    assert len(store2) == 1

    # file must not contain the raw token
    content = (tmp_path / "devices.json").read_text()
    assert token_b64 not in content
    assert token_hash in content


def test_revoke(tmp_path: Path):
    store = auth.DeviceStore(tmp_path / "devices.json")
    token_b64, token_hash = auth.generate_token()
    store.add("client-1", "Pixel", token_hash)
    assert store.remove("client-1")
    assert not store.verify_token("client-1", token_b64)
    assert not store.remove("client-1")


def test_tokens_are_unique():
    seen = {auth.generate_token()[0] for _ in range(50)}
    assert len(seen) == 50
