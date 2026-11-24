"""Known-answer tests ported from etebase-rs (crypto.rs / utils.rs)."""

from __future__ import annotations

from nacl.signing import VerifyKey
import pytest

from custom_components.etesync.etebase import crypto
from custom_components.etesync.etebase.crypto import (
    CryptoMac,
    CryptoManager,
    LoginCryptoManager,
    from_base64,
    get_padding,
    pad,
    pad_fixed,
    unpad,
    unpad_fixed,
)
from custom_components.etesync.etebase.errors import EncryptionError

PASSWORD = "SomePassword"
KEY = "Eq9b_rdbzeiU3P4sg5qN24KXbNgy8GgCeC74nFF99hI"
SALT = "6y7jUaojtLq6FISBWPjwXTeiYk5cTiz1oe6HVNGvn2E"
COL_CONTEXT = b"Col     "

# Subset of PRECALC from etebase-rs: deterministic_encrypt(b"<" * length).
DETERMINISTIC = {
    0: "Jp5B3loU3qoohgvlOuiYcbEI1JUhHzwfKsqRRvR_KZFQWvJFn07eHg",
    1: "yeX7EzjL43RCN89Ch5RBjWkmIj4GwFNgKJhKYEmbn0Crgey8ScixVzk",
    15: "EVy8E401Licx7Pjg_3YdC0Ei9xqtAqzFApm_gzA47-1SAZr7aJbwSuWQTVcTX-7pNquBLqtZ8A",
    31: "XfRyxsqIpNpVYTKLQqttbool_y55nTCLU0FtpYwKHrM8qxa-cOA8GzL8jds2E7PiLjXl5Q1L_id6ycQzfdBQ7WafB4hnqqw",
    32: "XTZco0R8H3bHobcXJ9c0-8KTRz-sqJQTtqiTk42mLzk4IhJyxv0aqpLX2kKnC3TXCLKWFEYZ1GZfczjeRtUCTYvxxFxzJVSD",
    63: "wDkNMUPUFESxc0Kz0jqUrm6BnPu9OYOJn8VSMc_YjamfRkJi5CSHWZmv-Ps__dg5dwOR1gzIg56z4SUfyBSR9nVpF34DpgoFEs3E69B8GdnjANpTY6swRA2hGnue2jBzRTQrWjwYbA",
}


@pytest.fixture
def crypto_manager() -> CryptoManager:
    """Return the crypto manager used by the upstream tests."""
    return CryptoManager(from_base64(KEY)[:32], COL_CONTEXT, 1)


def test_derive_key() -> None:
    """Argon2id derivation matches the reference."""
    assert crypto.derive_key(from_base64(SALT)[:16], PASSWORD) == from_base64(KEY)


def test_subkey_and_mac(crypto_manager: CryptoManager) -> None:
    """KDF sub keys, subkey derivation and MACs match the reference."""
    assert crypto_manager.derive_subkey(bytes(32)) == from_base64(
        "4w-VCSTETv26JjVlVlD2VaACcb6aQSD2JbF-e89xnaA"
    )
    assert crypto_manager.calculate_mac(bytes(32)) == from_base64(
        "bz6eMZdAkIuiLUuFDiVwuH3IFs4hYkRfhzang_JzHr8"
    )
    mac = crypto_manager.crypto_mac()
    mac.update(bytes(4))
    assert mac.finalize() == from_base64("y5nYZ75gDUna4bnaAHobXUlgQoTKOnueNW_KCYxcAg4")


def test_crypto_mac_len_prefix() -> None:
    """Length-prefixed MAC updates match the reference, keyed and unkeyed."""
    for key, expected in (
        (None, "P-Hpzo86RG6Ps4R1gGXmQrzmdJC2OotqqreKmB8G45A"),
        (from_base64(KEY), "rgL6d_XDiBfbzevFdtktc61XB5-PkS1uQ1cj5DgfFc8"),
    ):
        mac = CryptoMac(key)
        mac.update(bytes(4))
        mac.update_with_len_prefix(bytes(8))
        assert mac.finalize() == from_base64(expected)


@pytest.mark.parametrize(("length", "expected"), DETERMINISTIC.items())
def test_deterministic_encrypt(crypto_manager: CryptoManager, length: int, expected: str) -> None:
    """Deterministic encryption is byte-identical to the reference."""
    cipher = crypto_manager.deterministic_encrypt(b"<" * length)
    assert cipher == from_base64(expected)
    assert crypto_manager.deterministic_decrypt(cipher) == b"<" * length


def test_encrypt_roundtrip_and_tamper(crypto_manager: CryptoManager) -> None:
    """Combined and detached encryption round-trip; tampering is detected."""
    text = b"This Is Some Test Cleartext."
    cipher = crypto_manager.encrypt(text, b"ad")
    assert crypto_manager.decrypt(cipher, b"ad") == text
    with pytest.raises(EncryptionError):
        crypto_manager.decrypt(cipher, b"other ad")

    tag, detached = crypto_manager.encrypt_detached(text)
    assert crypto_manager.decrypt_detached(detached, tag) == text
    tampered = detached[:-1] + bytes([detached[-1] ^ 1])
    with pytest.raises(EncryptionError):
        crypto_manager.decrypt_detached(tampered, tag)


def test_login_signature_verifies() -> None:
    """Signatures from the login key verify with its public key."""
    login = LoginCryptoManager(bytes(32))
    msg = b"challenge"
    VerifyKey(login.pubkey).verify(msg, login.sign_detached(msg))


def test_padding() -> None:
    """Padding sizes and round trips match the reference."""
    assert all(get_padding(i) > i for i in range(1, 1 << 14))
    assert get_padding(2343242) == 2359296
    buf = bytes(1076)
    assert unpad(pad(buf)) == buf
    for length in range(64):
        data = b"<" * length
        assert unpad_fixed(pad_fixed(data, 32), 32) == data
