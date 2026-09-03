"""Registration state per line and the state.json the CLI reads (`ai-phone status`)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


class StateError(RuntimeError):
    pass


@dataclass
class LineStatus:
    id: str
    kind: str                    # trunk | sip
    address: str                 # the number or the AOR
    state: str                   # unregistered | registering | registered | degraded | skipped
    registrar: str | None
    expires_at: float | None
    last_error: str | None
    since: float


def next_state(prev: str, active: bool, code: int) -> str:
    if active:
        return "registered"
    if code == 0:
        return "unregistered"
    return "degraded"


class StateFile:
    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, lines: dict[str, LineStatus], active_call: dict | None, brain: dict) -> None:
        doc = {"written_at": time.time(), "lines": {k: asdict(v) for k, v in lines.items()},
               "active_call": active_call, "brain": brain}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(doc, f)
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise

    @staticmethod
    def read(path: Path) -> dict:
        try:
            return json.loads(Path(path).read_text())
        except FileNotFoundError:
            raise StateError(f"{path} does not exist (daemon never started?)") from None
