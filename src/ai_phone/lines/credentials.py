"""SIP credentials that satisfy both carriers: usernames alphanumeric 4–32 (Telnyx), passwords 12+ with a digit and mixed case (Twilio)."""
from __future__ import annotations

import secrets
import string


def generate_sip_user(line_id: str) -> str:
    base = "ai" + "".join(ch for ch in line_id if ch.isalnum())
    return (base[:28] + secrets.token_hex(2)).lower()


def generate_sip_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(24))
        if any(c.isdigit() for c in p) and any(c.isupper() for c in p) and any(c.islower() for c in p):
            return p
