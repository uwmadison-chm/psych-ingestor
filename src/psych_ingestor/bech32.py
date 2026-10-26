"""Bech32, the text encoding signing keys are written in.

It's what age writes its keys in, and Bitcoin its addresses: a short prefix that says what
the string is, a `1`, and the data in an alphabet of 32 characters chosen so that no two
look alike, ending in a six-character checksum. The checksum is the point. A key that
was cut short or mistyped while being copied fails to decode, rather than turning into a
different key.

This follows the reference implementation in BIP 173
(https://github.com/bitcoin/bips/blob/master/bip-0173.mediawiki), less its 90-character
limit, which age doesn't keep either, and the parts about Bitcoin addresses.
"""

from __future__ import annotations

ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
CHECKSUM_LENGTH = 6
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def encode(prefix: str, data: bytes) -> str:
    """`data` as a lowercase bech32 string starting `{prefix}1`."""
    prefix = prefix.lower()
    groups = _regroup(list(data), 8, 5, pad=True)
    checksum = _checksum(prefix, groups)
    return prefix + "1" + "".join(ALPHABET[group] for group in groups + checksum)


def decode(text: str) -> tuple[str, bytes]:
    """The prefix and data of a bech32 string, or ValueError saying what's wrong.

    Either case is accepted, as long as it's one or the other: age writes private keys
    in capitals, and everything else in lowercase.
    """
    if text != text.lower() and text != text.upper():
        raise ValueError("it mixes capital and lowercase letters")
    text = text.lower()
    separator = text.rfind("1")
    if separator < 1 or len(text) - separator - 1 < CHECKSUM_LENGTH:
        raise ValueError("it's too short to be a key")
    prefix, body = text[:separator], text[separator + 1 :]
    if any(character not in ALPHABET for character in body):
        raise ValueError("it has a character that can't be part of a key")
    groups = [ALPHABET.index(character) for character in body]
    if _polymod(_expand(prefix) + groups) != 1:
        raise ValueError("its checksum doesn't match")
    return prefix, bytes(_regroup(groups[:-CHECKSUM_LENGTH], 5, 8, pad=False))


def _polymod(values: list[int]) -> int:
    check = 1
    for value in values:
        top = check >> 25
        check = (check & 0x1FFFFFF) << 5 ^ value
        for bit, generator in enumerate(_GENERATOR):
            if (top >> bit) & 1:
                check ^= generator
    return check


def _expand(prefix: str) -> list[int]:
    """The prefix as the checksum sees it, so a string with its prefix changed fails."""
    return [ord(c) >> 5 for c in prefix] + [0] + [ord(c) & 31 for c in prefix]


def _checksum(prefix: str, groups: list[int]) -> list[int]:
    polymod = _polymod(_expand(prefix) + groups + [0] * CHECKSUM_LENGTH) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(CHECKSUM_LENGTH)]


def _regroup(values: list[int], from_bits: int, to_bits: int, pad: bool) -> list[int]:
    """Bytes into 5-bit groups (padding the last), or 5-bit groups back into bytes
    (refusing leftover bits that aren't zero padding)."""
    accumulator, bits, regrouped = 0, 0, []
    largest = (1 << to_bits) - 1
    for value in values:
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            regrouped.append((accumulator >> bits) & largest)
    if pad and bits:
        regrouped.append((accumulator << (to_bits - bits)) & largest)
    elif not pad and (bits >= from_bits or (accumulator << (to_bits - bits)) & largest):
        raise ValueError("it has bits left over at the end")
    return regrouped
