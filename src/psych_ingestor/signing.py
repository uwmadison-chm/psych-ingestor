"""Parameter signing: proof that a run's parameters are the ones you sent.

A participant's link is in their hands, and they can edit it. A task that sets
`signed_parameters` only starts a run when the link carries a signature over exactly
those parameters' values, made with a private key on the secure side. The web server has
only the public key, which can check a signature but not make one. See issue #15.

What's signed is the values, not the link. Pig never sees the address a participant
followed, only the parameters the task sends it when it starts a run, so the signature
covers those: the task code, the key's name, and each signed parameter's name and value.
Pig builds that message itself, on both sides (`pigsty sign` and `pig serve`), so a
participant's browser never has to put anything in a canonical order.

The signature travels as one more parameter, `_sig`, written `{key name}.{signature}`.
A key name never has a dot in it, and the signature is base64url, which doesn't either.

Signatures don't expire. An offline participant can do a whole run before their device
reaches Pig, and an expired signature would leave that data stuck on the device. Taking a
key out of a task's `signed_by` stops every link signed with it.
"""

from __future__ import annotations

import base64
import binascii
import json
import re

from cryptography.exceptions import InvalidSignature

from . import keys

# The parameter a signature travels in. Every parameter name starting `_` is Pig's (see
# `config.RESERVED_PARAMETER_PREFIX`), so a task's own can never collide with it.
SIGNATURE_PARAMETER = "_sig"

# Bumped if what's signed ever changes, so an old signature can't be read as a new one.
MESSAGE_VERSION = 1

_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


class BadSignature(Exception):
    """A signature that doesn't check out. The message says why, for whoever made the
    link."""


def message(task_code: str, key_name: str, values: dict[str, str]) -> bytes:
    """What gets signed: the task, the key, and the signed parameters' values.

    The task code is in it so a link signed for one task can't start another, and the key
    name so a signature can't be passed off as another key's. JSON with sorted keys, no
    spaces, and only ASCII is the same bytes for the same values however they were put
    together.
    """
    return json.dumps(
        {"v": MESSAGE_VERSION, "task": task_code, "key": key_name, "params": values},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def sign(key: keys.SigningKey, task_code: str, values: dict[str, str]) -> str:
    """The `_sig` value for these parameters."""
    signature = key.private.sign(message(task_code, key.name, values))
    encoded = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{key.name}.{encoded}"


def verify(
    signature: object,
    task_code: str,
    values: dict[str, str],
    accepted: dict[str, str],
) -> str:
    """Check a `_sig` against the values it should cover, and return the name of the
    key that signed it. `accepted` is the task's `signed_by`: each key's name and its
    public key.

    Raises BadSignature, saying what's wrong, for a signature that's missing, can't be
    read, is by a key the task doesn't accept, or doesn't match the values.
    """
    if signature is None:
        raise BadSignature(f"there's no {SIGNATURE_PARAMETER} parameter.")
    if not isinstance(signature, str):
        raise BadSignature(f"{SIGNATURE_PARAMETER} has to be text.")
    key_name, _, encoded = signature.rpartition(".")
    try:
        keys.check_key_name(key_name)
    except ValueError:
        raise BadSignature(
            f"{SIGNATURE_PARAMETER} should be a key name, a dot, and a signature, and "
            "this one isn't. It may have been cut short when the link was copied."
        ) from None
    signature = _decode(encoded)
    if key_name not in accepted:
        raise BadSignature(
            f"it was signed with the key {key_name}, which this task doesn't accept."
        )
    try:
        keys.signing_public_key(accepted[key_name]).verify(
            signature, message(task_code, key_name, values)
        )
    except InvalidSignature:
        raise BadSignature(
            "the signature doesn't match. A value in the link was changed after it was "
            "signed, or it was signed for another task or with other parameters."
        ) from None
    return key_name


def _decode(encoded: str) -> bytes:
    if _BASE64URL.match(encoded):
        try:
            signature = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (binascii.Error, ValueError):
            signature = b""
        if len(signature) == 64:
            return signature
    raise BadSignature(
        f"the signature in {SIGNATURE_PARAMETER} can't be read. It may have been cut "
        "short when the link was copied."
    )
