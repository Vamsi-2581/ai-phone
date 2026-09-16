"""Checks the daemon applies before dialling: emergency numbers are never called; an optional daily limit."""
from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable

EMERGENCY_CODES = frozenset({"000", "100", "101", "102", "103", "108", "110", "112", "118", "119", "190", "911", "999"})
ACCESS_PREFIXES = ("00", "011")                   # international access prefixes dialled before a country code
_DIALLABLE = re.compile(r"[+0-9._\-()\s]+")       # no letters: the dialler may treat it as a phone number


def is_emergency(target: str) -> bool:
    """True when the target's digits are an emergency code, alone or behind a 1–3 digit country code, optionally
    after one international access prefix. A target (or sip: user part) with any letter in it is not a number."""
    user = target[4:].split("@", 1)[0] if target.startswith("sip:") else target
    if not _DIALLABLE.fullmatch(user):
        return False
    digits = re.sub(r"\D", "", user)
    candidates = [digits] + [digits[len(p):] for p in ACCESS_PREFIXES if digits.startswith(p)]
    return any(_is_code(d) for d in candidates)


def _is_code(digits: str) -> bool:
    return digits in EMERGENCY_CODES or any(len(digits) > cc and digits[cc:] in EMERGENCY_CODES for cc in (1, 2, 3))


class DailyCounter:
    """Outbound calls started today (local date), in a small file so a restart does not reset the limit."""

    def __init__(self, path: Path, limit: int, today: Callable[[], datetime.date] = datetime.date.today):
        self.path, self.limit, self._today = Path(path), limit, today

    def _read(self, today: datetime.date | None = None) -> int:
        """Today's count. A missing or unreadable file, another day's count, or a count that is not a non-negative
        whole number all read as 0."""
        day = (today if today is not None else self._today()).isoformat()
        try:
            doc = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return 0
        if not isinstance(doc, dict) or doc.get("date") != day:
            return 0
        count = doc.get("count")
        return count if type(count) is int and count >= 0 else 0   # not bool, float, str or a negative number

    def allowed(self) -> bool:
        return self.limit <= 0 or self._read() < self.limit

    def record(self) -> int:
        today = self._today()                     # once, so the count read and the date written agree at midnight
        n = self._read(today) + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".calls-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"date": today.isoformat(), "count": n}, f)
                f.flush()
                os.fsync(f.fileno())              # a power loss must not leave an empty file (which reads as 0)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return n
