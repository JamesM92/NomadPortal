"""LXMF addresses as people share them.

A destination hash is 32 hex characters. It travels in a few wrappers:

- the bare hash;
- ``lxmf@<hash>`` (also ``lxmf://<hash>`` and ``<hash>`` in angle brackets),
  the form Sideband and MeshChat show;
- ``lxma://<hash>:<public key>``, the link in this portal's own QR code
  (Columba's scheme, see identity_qr.py).

The last one matters because it carries the person's public key. A
destination hash cannot be turned back into a key, so without one the portal
can only message someone after hearing them announce, which may take hours or
never happen. With the key, ``remember_identity`` seeds the local Reticulum
cache and the first message can go out at once.

That is only safe if the key is checked: a hash is derived from the key, so
the portal rebuilds the hash from the key it was given and refuses the link
unless they match. Without that check anyone could plant a false key for any
address and read, or impersonate, that person.
"""

import re
from dataclasses import dataclass
from typing import Optional

_HASH_RE = re.compile(r"[0-9a-f]{32}")
_KEY_RE = re.compile(r"[0-9a-f]{128}")          # a 64-byte Reticulum public key

_HASH_HELP = ("An LXMF address is 32 hexadecimal characters. It may be written "
              "as lxmf@<address> or as an lxma:// link.")


@dataclass(frozen=True)
class LxmfAddress:
    dest_hash: str                       # 32 lowercase hex characters
    public_key: Optional[str] = None     # 128 lowercase hex characters, if the link carried one


def parse_lxmf_address(text) -> LxmfAddress:
    """Read an address in any of the accepted forms. Raises ValueError with
    a message a person can act on."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Enter an address.")
    t = re.sub(r"\s+", "", text).lower().replace("<", "").replace(">", "")

    key = None
    if t.startswith("lxma://"):
        rest = t[len("lxma://"):]
        hash_part, sep, key_part = rest.partition(":")
        if not _HASH_RE.fullmatch(hash_part):
            raise ValueError(_HASH_HELP)
        if not sep or not _KEY_RE.fullmatch(key_part):
            raise ValueError("An lxma:// link must end with the person's public key "
                             "(128 hexadecimal characters) after the address.")
        return LxmfAddress(hash_part, key_part)

    for prefix in ("lxmf://", "lxmf@", "lxmf:"):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    if not _HASH_RE.fullmatch(t):
        raise ValueError(_HASH_HELP)
    return LxmfAddress(t, key)


def remember_identity(rns, address: LxmfAddress) -> bool:
    """Teach the local Reticulum cache the public key from an ``lxma://``
    link, so a message can be sent before the person announces. Returns
    False when the address carries no key. Raises ValueError when the key
    is not valid or does not belong to the address; nothing is stored then.
    ``rns`` is the RNS module (passed in so tests need no network)."""
    if not address.public_key:
        return False
    dest = bytes.fromhex(address.dest_hash)
    pub = bytes.fromhex(address.public_key)
    try:
        ident = rns.Identity(create_keys=False)
        ident.load_public_key(pub)
        expected = rns.Destination.hash_from_name_and_identity("lxmf.delivery", ident)
    except Exception:
        raise ValueError("That public key is not valid.") from None
    if expected != dest:
        raise ValueError("The public key does not belong to this address, so it was not used.")
    rns.Identity.remember(None, dest, pub)
    return True
