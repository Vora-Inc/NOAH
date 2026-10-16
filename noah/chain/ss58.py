"""Chain addresses, compared by what they are rather than how they are written.

The Ark's address is configuration. Compared as text, every ordinary way of
writing the same key down produces a mismatch, and a mismatch here is not an
error. It is every deposit ever made quietly failing to be a deposit, on a vault
nobody can withdraw from.

Four ways the same key arrives looking different: written for another network
prefix it is a different string entirely, one starting with a one rather than a
five; out of a configuration file it carries a trailing newline; from a decoder
it may be hex rather than an address; and a single mistyped character stays
inside the alphabet and reads as a perfectly good address.

An address is a network prefix, a thirty-two byte public key, and a two byte
checksum, written in base58. The checksum is the first two bytes of a blake2b
digest over the prefix and key with a fixed salt.

Standard library only, deliberately. This package declares no runtime
dependencies, on the stated grounds that nothing third-party belongs between a
fixture and a score, and an address comparison sits on exactly that path.
"""

import hashlib

from .sources import Refusal

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_VALUE = {character: index for index, character in enumerate(_ALPHABET)}
_CHECKSUM_SALT = b"SS58PRE"
_PUBLIC_KEY_BYTES = 32
_CHECKSUM_BYTES = 2
_MAX_ADDRESS_TEXT_LENGTH = 64


class NotAnAddress(Refusal):
    """Text that is not a chain address, or is one written wrongly."""


def _base58_bytes(text):
    if len(text) > _MAX_ADDRESS_TEXT_LENGTH:
        raise NotAnAddress(
            f"a value of {len(text)} characters (not shown) is longer than any genuine "
            f"address of this shape can ever be ({_MAX_ADDRESS_TEXT_LENGTH} is a "
            f"comfortable bound above the true 50-character worst case for a "
            f"2-byte-prefix, 32-byte-key, 2-byte-checksum address) -- refusing before "
            f"decoding it, not after, so a very long string cannot be used to force "
            f"disproportionate CPU or memory work building one large integer."
        )
    total = 0
    for character in text:
        value = _VALUE.get(character)
        if value is None:
            raise NotAnAddress(
                f"a {len(text)}-character value (not shown) is not an address: it contains a "
                f"character outside the address alphabet, which addresses "
                f"never do. A trailing newline out of a configuration file is the likeliest "
                f"cause and the hardest to see."
            )
        total = total * 58 + value
    body = total.to_bytes((total.bit_length() + 7) // 8, "big")
    leading = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading + body


def _base58_text(raw):
    """The inverse of ``_base58_bytes`` above: raw bytes to base58 text,
    leading zero bytes surviving as leading ``'1'`` characters the same way
    ``_base58_bytes`` reads them back out. Kept beside its inverse so the
    alphabet is declared once and the two can never drift apart."""
    number = int.from_bytes(raw, "big")
    text = ""
    while number > 0:
        number, remainder = divmod(number, 58)
        text = _ALPHABET[remainder] + text
    leading = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading + text


_NETWORK = 42


def address_of(public_key):
    """Encode exactly 32 public-key bytes as a checksummed network-42 SS58 address."""
    if not isinstance(public_key, (bytes, bytearray)) or len(public_key) != _PUBLIC_KEY_BYTES:
        raise NotAnAddress(
            f"a {_PUBLIC_KEY_BYTES}-byte public key is needed to write an address, got "
            f"{public_key!r}."
        )
    body = bytes([_NETWORK]) + bytes(public_key)
    checksum = hashlib.blake2b(_CHECKSUM_SALT + body, digest_size=64).digest()[:_CHECKSUM_BYTES]
    return _base58_text(body + checksum)


def public_key_of(address):
    """The public key behind an address, or a refusal.

    Two addresses are the same account when their public keys match. Nothing
    else about the text is meaningful, and comparing the text instead is the
    silent failure this module exists to remove.
    """
    if not isinstance(address, str) or not address:
        raise NotAnAddress(f"an address came in as a {type(address).__name__}, which is not text.")

    raw = _base58_bytes(address)
    smallest = _PUBLIC_KEY_BYTES + _CHECKSUM_BYTES + 1
    if len(raw) < smallest:
        raise NotAnAddress(
            f"an address (the value is not shown) decodes to {len(raw)} bytes, too few to be "
            f"an address. An address "
            f"carries a network prefix, a {_PUBLIC_KEY_BYTES} byte public key and a "
            f"{_CHECKSUM_BYTES} byte checksum."
        )

    if raw[0] < 64:
        prefix_length = 1
    elif raw[0] < 128:
        prefix_length = 2
    else:
        raise NotAnAddress(
            f"an address (the value is not shown) decodes to a first byte of {raw[0]}, "
            f"which no genuine "
            f"SS58 encoder ever produces: a one-byte network prefix is below 64, and "
            f"a two-byte prefix's first byte is always 64-127 (its top two bits are "
            f"fixed at 01 by the encoding itself). A correct checksum for an invalid "
            f"prefix byte is still not a genuine address."
        )
    body, checksum = raw[:-_CHECKSUM_BYTES], raw[-_CHECKSUM_BYTES:]
    public_key = body[prefix_length:]
    if len(public_key) != _PUBLIC_KEY_BYTES:
        raise NotAnAddress(
            f"an address (the value is not shown) carries a {len(public_key)} byte key, "
            f"not {_PUBLIC_KEY_BYTES}."
        )

    digest = hashlib.blake2b(_CHECKSUM_SALT + body, digest_size=64).digest()
    if digest[:_CHECKSUM_BYTES] != checksum:
        raise NotAnAddress(
            "an address (the value is not shown) fails its own checksum, so it is not an "
            "address anybody holds. A "
            "single mistyped character stays inside the alphabet and reads as a good "
            "address until this is tested."
        )
    return public_key


def address_text(value):
    """``value`` for a message or a result, if it is a genuine address; else a fixed text."""
    try:
        public_key_of(value)
    except (NotAnAddress, TypeError, ValueError):
        return "(a value that is not shown)"
    return value
