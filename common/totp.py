# -*- coding: utf-8 -*-
"""Small RFC 6238 TOTP helper shared by registration and the panel."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


def totp_code(secret: str, timestamp: float | None = None) -> str:
    """Generate the six-digit SHA-1 TOTP code for a Base32 secret."""
    normalized = "".join(str(secret or "").split()).upper()
    if not normalized:
        raise ValueError("empty TOTP secret")
    padded = normalized + "=" * (-len(normalized) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
    except Exception as error:
        raise ValueError("invalid TOTP secret") from error
    counter = int((time.time() if timestamp is None else timestamp) // 30)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"
