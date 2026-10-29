"""Keys for end-to-end encryption and for parameter signing: public keys on the web
side, private keys on the secure side, and the one file that carries the first from the
second to the first.

A task that encrypts has its events and media parts encrypted in the participant's
browser, to a public key the web server hands out. The matching private key is on the
secure side only, and nothing on the web server can read the data. See issue #10.

A task that checks signatures only starts a run whose parameters were signed with a
private key on the secure side. The web server has the public key, which can check a
signature but not make one, so nothing on the web server can make a link that works.
See `signing.py` and issue #15.

The encryption is age (https://age-encryption.org). age calls a public key a
*recipient* and a private key an *identity*; Pig says public key and private key
everywhere, and this is the one place that mentions age's words. Signing is Ed25519,
with keys written the way age writes its own, in bech32, but with prefixes of their own,
so the two kinds can't be mistaken for each other.

- **Secure side.** `private_keys_dir` in `pigsty.toml` is a directory of `.key` files,
  one private key each. An encryption key is in the format `age-keygen` writes, and a
  signing key is the same but for its prefix. A key's name is its path inside the
  directory without `.key`, so `lucida/2026a.key` is `lucida/2026a`. `pigsty keys new`
  makes one of either kind; `pigsty keys export` writes the public keys file, with both.
- **Web side.** `public_keys_file` in `pig.toml`, or in a study's file in one of its
  include directories, is that exported file, copied over whole. A task's `encrypt_to`
  names one of its encryption keys, and its `signed_by` names signing keys.

Nothing here reads either configuration file. The callers say which files to read.
"""

from __future__ import annotations

import base64
import binascii
import importlib.metadata
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

import pyrage
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from pyrage import x25519

from . import bech32
from .config import SAFE_VALUE_EXPLANATION, ConfigurationError, is_safe_value

KEY_SUFFIX = ".key"

# Every age file starts with this line, and nothing that isn't an age file does. It's
# what the web server checks an encrypted event or part for: not that the right key was
# used, which it can't know, but that the browser encrypted at all.
AGE_HEADER = b"age-encryption.org/v1\n"

PUBLIC_KEY_PREFIX = "age1"
PRIVATE_KEY_PREFIX = "AGE-SECRET-KEY-1"

# A signing key's prefixes, before the bech32 `1`. Each is followed by the 32 bytes of an
# Ed25519 key: the public key itself, or the seed the private key is made from.
SIGNING_PUBLIC_PREFIX = "pigpk"
SIGNING_PRIVATE_PREFIX = "PIG-SIGNING-KEY-"
SIGNING_PUBLIC_KEY_PREFIX = SIGNING_PUBLIC_PREFIX + "1"
SIGNING_PRIVATE_KEY_PREFIX = SIGNING_PRIVATE_PREFIX + "1"


def check_key_name(name: str) -> None:
    """A key name is one or more safe values joined by `/`, like `lucida/2026a`. Raises
    ValueError saying what's wrong."""
    parts = name.split("/")
    if not name or not all(is_safe_value(part) for part in parts):
        raise ValueError(
            f"{name!r} isn't usable as a key name. A key name is one or more parts "
            f"separated by '/', like lucida/2026a, each made of {SAFE_VALUE_EXPLANATION}."
        )


# ------------------------------------------------------------------ public keys


def read_public_keys(path: Path) -> dict[str, str]:
    """Read a public keys file: each key's name and the public key itself.

    Blank lines and lines starting with `#` are skipped. Every other line is a name and
    a public key separated by a space, as `pigsty keys export` writes them. Raises
    ConfigurationError for anything else, saying which line.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"Can't read {path}: {error}") from error

    keys: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{path}, line {number}"
        fields = line.split()
        if len(fields) != 2:
            raise ConfigurationError(
                f"{where}: expected a key name and a public key, like "
                "'lucida/2026a age1…'. This file is written by pigsty keys export; "
                "export it again rather than editing it."
            )
        name, public_key = fields
        try:
            check_key_name(name)
        except ValueError as error:
            raise ConfigurationError(f"{where}: {error}") from None
        if name in keys:
            raise ConfigurationError(f"{where}: {name} is listed twice.")
        try:
            check_public_key(public_key)
        except ValueError as error:
            raise ConfigurationError(f"{where}: {error}") from None
        keys[name] = public_key
    return keys


def check_public_key(public_key: str) -> None:
    """Raise ValueError unless this is a well-formed public key, of either kind. The
    checksum built into the format catches a key that was cut short or mistyped."""
    if is_signing_public_key(public_key):
        signing_public_key(public_key)
        return
    if not public_key.startswith(PUBLIC_KEY_PREFIX):
        raise ValueError(
            f"{public_key[:12]!r}… isn't a public key Pig can use. Public keys start "
            f"with {PUBLIC_KEY_PREFIX}, for encryption, or "
            f"{SIGNING_PUBLIC_KEY_PREFIX}, for signing."
        )
    try:
        x25519.Recipient.from_str(public_key)
    except pyrage.RecipientError as error:
        raise ValueError(
            f"{public_key[:12]}… isn't a whole public key ({error}). It may have been "
            "cut short when it was copied."
        ) from None


def is_signing_public_key(public_key: str) -> bool:
    """Whether a public key is for checking signatures, rather than encrypting to."""
    return public_key.startswith(SIGNING_PUBLIC_KEY_PREFIX)


def signing_public_key(public_key: str) -> Ed25519PublicKey:
    """A signing public key, from the way the public keys file writes it, or ValueError
    saying what's wrong with it."""
    try:
        prefix, raw = bech32.decode(public_key)
    except ValueError as error:
        raise ValueError(
            f"{public_key[:12]}… isn't a whole public key ({error}). It may have been "
            "cut short when it was copied."
        ) from None
    if prefix != SIGNING_PUBLIC_PREFIX or len(raw) != 32:
        raise ValueError(f"{public_key[:12]}… isn't a signing public key.")
    return Ed25519PublicKey.from_public_bytes(raw)


# ------------------------------------------------------------------ ciphertext


def is_encrypted_text(value: object) -> bool:
    """Whether an event's `data` is what an encrypting task sends: the base64 of an age
    file, as a string."""
    if not isinstance(value, str):
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return decoded.startswith(AGE_HEADER)


def is_encrypted_bytes(start: bytes) -> bool:
    """Whether the first bytes of a media part are the start of an age file."""
    return start.startswith(AGE_HEADER)


# ------------------------------------------------------------------ private keys


@dataclass(frozen=True)
class PrivateKey:
    """A private key for decrypting."""

    name: str
    path: Path
    identity: x25519.Identity

    @property
    def public_key(self) -> str:
        return str(self.identity.to_public())


@dataclass(frozen=True)
class SigningKey:
    """A private key for signing parameters."""

    name: str
    path: Path
    private: Ed25519PrivateKey

    @property
    def public_key(self) -> str:
        raw = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return bech32.encode(SIGNING_PUBLIC_PREFIX, raw)


def read_private_keys(directory: Path) -> list[PrivateKey]:
    """Every private key for decrypting in a directory and the directories inside it,
    sorted by name.

    Raises ConfigurationError for a key file that's readable by anyone but its owner, or
    that isn't a private key of either kind, or for anything in the directory that's a
    symbolic link.
    """
    return [key for key in read_all_keys(directory) if isinstance(key, PrivateKey)]


def read_signing_keys(directory: Path) -> list[SigningKey]:
    """Every signing key in a directory and the directories inside it, sorted by name.
    Raises ConfigurationError as `read_private_keys` does."""
    return [key for key in read_all_keys(directory) if isinstance(key, SigningKey)]


def read_all_keys(directory: Path) -> list[PrivateKey | SigningKey]:
    """Every private key in a directory, of both kinds, sorted by name. Raises
    ConfigurationError as `read_private_keys` does."""
    if not directory.is_dir():
        raise ConfigurationError(
            f"private_keys_dir is {directory}, and there's no directory there."
        )
    keys: list[PrivateKey | SigningKey] = []
    for path in sorted(directory.rglob(f"*{KEY_SUFFIX}")):
        if path.is_symlink():
            raise ConfigurationError(
                f"{path} is a symbolic link. Put the key file itself in "
                "private_keys_dir."
            )
        if not path.is_file():
            continue
        name = PurePosixPath(path.relative_to(directory).as_posix()).as_posix()
        name = name.removesuffix(KEY_SUFFIX)
        try:
            check_key_name(name)
        except ValueError as error:
            raise ConfigurationError(f"{path}: {error}") from None
        _check_permissions(path)
        keys.append(_read_key_file(name, path))
    return keys


def read_private_key_file(path: Path) -> PrivateKey:
    """One private key file named on the command line, for a restore test."""
    _check_permissions(path)
    key = _read_key_file(path.stem, path)
    if not isinstance(key, PrivateKey):
        raise ConfigurationError(
            f"{path} is a signing key. Decrypting needs a key made by pigsty keys new "
            "without --signing."
        )
    return key


def _check_permissions(path: Path) -> None:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigurationError(
            f"{path} can be read by people other than its owner (its mode is "
            f"{stat.filemode(mode)}). A private key has to be private: "
            f"chmod 600 {path}"
        )


def _read_key_file(name: str, path: Path) -> PrivateKey | SigningKey:
    """A key file of either kind, told apart by the prefix on its one key line."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"Can't read {path}: {error}") from error
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(lines) == 1 and lines[0].startswith(SIGNING_PRIVATE_KEY_PREFIX):
        return SigningKey(name, path, _read_signing_key(path, lines[0]))
    if len(lines) != 1 or not lines[0].startswith(PRIVATE_KEY_PREFIX):
        raise ConfigurationError(
            f"{path} isn't a private key file. It should hold one line starting "
            f"{PRIVATE_KEY_PREFIX} (for decrypting) or {SIGNING_PRIVATE_KEY_PREFIX} (for "
            "signing), and any number of lines starting with #."
        )
    try:
        return PrivateKey(name, path, x25519.Identity.from_str(lines[0]))
    except pyrage.IdentityError as error:
        raise ConfigurationError(
            f"{path} has a private key that can't be read ({error}). It may have been "
            "cut short when it was copied."
        ) from None


def _read_signing_key(path: Path, line: str) -> Ed25519PrivateKey:
    try:
        prefix, seed = bech32.decode(line)
    except ValueError as error:
        raise ConfigurationError(
            f"{path} has a signing key that can't be read ({error}). It may have been "
            "cut short when it was copied."
        ) from None
    if prefix != SIGNING_PRIVATE_PREFIX.lower() or len(seed) != 32:
        raise ConfigurationError(f"{path} has a signing key that can't be read.")
    return Ed25519PrivateKey.from_private_bytes(seed)


def new_private_key(directory: Path, name: str, now: datetime) -> PrivateKey:
    """Make a new private key and write it to `{directory}/{name}.key`, readable only by
    its owner. Refuses to replace a key that's already there.

    The file is the one `age-keygen` writes, with one more comment line saying what
    made it, so the stock `age` command can always read it: `age -d -i lucida/2026a.key`.
    """
    check_key_name(name)
    path = directory / f"{name}{KEY_SUFFIX}"
    identity = x25519.Identity.generate()
    _write_key_file(
        path,
        f"# created: {now.replace(microsecond=0).isoformat()}\n"
        f"# made by pigsty keys new, from psych-ingestor {_version('psych-ingestor')} "
        f"using pyrage {_version('pyrage')}\n"
        f"# public key: {identity.to_public()}\n"
        f"{identity}\n",
    )
    return PrivateKey(name, path, identity)


def new_signing_key(directory: Path, name: str, now: datetime) -> SigningKey:
    """Make a new signing key and write it to `{directory}/{name}.key`, readable only by
    its owner. Refuses to replace a key that's already there.

    The file is laid out like an age key file, so the two kinds sit side by side in
    `private_keys_dir`, but age can't read this one.
    """
    check_key_name(name)
    path = directory / f"{name}{KEY_SUFFIX}"
    private = Ed25519PrivateKey.generate()
    key = SigningKey(name, path, private)
    seed = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    _write_key_file(
        path,
        f"# created: {now.replace(microsecond=0).isoformat()}\n"
        f"# made by pigsty keys new --signing, from psych-ingestor "
        f"{_version('psych-ingestor')} using cryptography {_version('cryptography')}\n"
        f"# public key: {key.public_key}\n"
        f"{bech32.encode(SIGNING_PRIVATE_PREFIX, seed).upper()}\n",
    )
    return key


def _write_key_file(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Made with its final permissions, so it's never readable by anyone else even for a
    # moment, and with O_EXCL so it can't replace a key that's there.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as written:
        written.write(text)
        written.flush()
        os.fsync(written.fileno())


def public_keys_text(
    keys: Sequence[PrivateKey | SigningKey], directory: Path, now: datetime
) -> str:
    """The public keys file for these private keys, of both kinds, as `pigsty keys
    export` prints it."""
    lines = [
        "# Public keys, for public_keys_file in pig.toml.",
        f"# Written by pigsty keys export from {directory} on {now.date().isoformat()}.",
        "# Don't edit this file. Make or remove keys there, then export again.",
    ]
    lines += [f"{key.name} {key.public_key}" for key in sorted(keys, key=_by_name)]
    return "\n".join(lines) + "\n"


def _by_name(key: PrivateKey | SigningKey) -> str:
    return key.name


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "(unknown version)"


# ------------------------------------------------------------------ decrypting


class CantDecrypt(Exception):
    """Nothing this keyring holds opens it."""


class Keyring:
    """The private keys decryption tries, in order, starting with whichever one opened
    the last thing it decrypted.

    age doesn't say which key a file was encrypted to, by design, so decrypting means
    trying keys until one works. A run is nearly always encrypted to one key, so after
    the first event, the first key tried is the one that works. Each wrong key costs
    about 75 µs.
    """

    def __init__(self, keys: list[PrivateKey]):
        self._keys = list(keys)
        self.used: set[str] = set()

    def __bool__(self) -> bool:
        return bool(self._keys)

    def decrypt(self, ciphertext: bytes) -> bytes:
        for number, key in enumerate(self._keys):
            try:
                plaintext = pyrage.decrypt(ciphertext, [key.identity])
            except pyrage.DecryptError:
                continue
            if number:
                self._keys.insert(0, self._keys.pop(number))
            self.used.add(key.name)
            return plaintext
        raise CantDecrypt()
