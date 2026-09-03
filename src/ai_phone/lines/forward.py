"""GSM MMI divert codes (ETSI TS 122 030): register-and-activate with **, check with *#, cancel with ##."""
from __future__ import annotations

WHEN = ("all", "busy", "no-answer", "unreachable")
_CODE = {"all": "21", "busy": "67", "no-answer": "61", "unreachable": "62"}


def divert_codes(when: str, number_e164: str) -> dict:
    if when not in _CODE:
        raise ValueError(f"when must be one of {WHEN}")
    c = _CODE[when]
    return {"activate": f"**{c}*{number_e164}#", "check": f"*#{c}#", "cancel": f"##{c}#", "cancel_all": "##002#"}
