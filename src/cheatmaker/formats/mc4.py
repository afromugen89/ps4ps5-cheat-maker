""".mc4 cheat files: a .shn, PKCS#7-padded, AES-256-CBC encrypted, base64 encoded.

Layout, key and IV as in ReaperPy ``cheatfmt.py`` (MIT; layout facts from
PS5Debug-studio, Apache-2.0). The key and IV below are the public ones every mc4 tool
uses: they only make the file unreadable to a text editor and protect nothing, so
having them in GPL source is fine (spec-pro open question 7).

The decrypted text is exactly ``shn.dumps`` of the same cheats (AC-12.2); cheat names
are sanitised there for both formats (architecture-pro decision 6).
"""

from __future__ import annotations

import base64
import binascii
import logging
import re

from cheatmaker.core.cheats import CheatFile
from cheatmaker.errors import CheatFileError
from cheatmaker.formats import _aes, shn

log = logging.getLogger(__name__)

KEY = b"304c6528f659c766110239a51cl5dd9c"  # 32 bytes: AES-256 (public, see above)
IV = b"u@}kzW2u[u(8DWar"  # 16 bytes
_SPACE = re.compile(rb"\s+")


def encrypt(plain: bytes) -> bytes:
    """mc4 bytes for ``plain`` (the .shn text)."""
    return base64.b64encode(_aes.cbc_encrypt(KEY, IV, _aes.pkcs7_pad(plain)))


def decrypt(data: bytes) -> bytes:
    """The .shn bytes inside an mc4 file. Raises CheatFileError when it isn't one."""
    try:
        raw = base64.b64decode(_SPACE.sub(b"", bytes(data)), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CheatFileError(f"not an mc4 file (base64): {exc}") from None
    if not raw or len(raw) % _aes.BLOCK:
        raise CheatFileError("not an mc4 file (its length doesn't fit AES)")
    plain = _aes.cbc_decrypt(KEY, IV, raw)
    try:
        plain = _aes.pkcs7_unpad(plain)
    except ValueError:
        # Some tools may not pad; the <Trainer check below decides.
        log.warning("action=mc4_load reason=bad padding; reading it unpadded")
    if b"<Trainer" not in plain:
        raise CheatFileError("can't decrypt this mc4 file (wrong key, or not an mc4 file)")
    return plain


def dumps(cf: CheatFile) -> bytes:
    return encrypt(shn.dumps(cf))


def loads(data: bytes | str) -> CheatFile:
    raw = data.encode("ascii", "replace") if isinstance(data, str) else data
    return shn.loads(decrypt(raw))
