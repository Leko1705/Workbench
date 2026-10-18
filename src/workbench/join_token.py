"""Join tokens: a workbench URL and an invite code packed into one copy-and-paste string.

    wbj1.<base64url(json {"u": url, "c": code})>

The invite code stays the only secret; the token is just a convenient wrapper, so it is
not signed. Treat it like the invite code itself.
"""
from __future__ import annotations

import base64
import json

PREFIX = "wbj1."
MAX_LEN = 4096


def encode_join_token(base_url: str, invite_code: str) -> str:
    raw = json.dumps({"u": base_url, "c": invite_code}, separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_join_token(token: str) -> tuple[str, str]:
    """Returns (base_url, invite_code); raises ValueError if the token is malformed."""
    token = "".join(token.split())          # tolerate line breaks added by chat apps/terminals
    if not token.startswith(PREFIX) or len(token) > MAX_LEN:
        raise ValueError("not a join token")
    body = token[len(PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        url, code = data["u"], data["c"]
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("not a join token") from e
    if not (isinstance(url, str) and isinstance(code, str) and url and code):
        raise ValueError("not a join token")
    return url, code
