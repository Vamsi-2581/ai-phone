"""Provisioning journal: every carrier step is written `started` (with the names it will use) before the request
and `done` (with the ids) after, so a crash anywhere is resumable and never duplicates resources."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

LINE_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")  # same rule as config.LINE_ID_RE; enforced here too because Journal.open is public
STATES = ("pending", "ready", "failed", "rollback_failed")


def _private_dir(d: Path) -> Path:
    if d.is_symlink():
        raise RuntimeError(f"{d} is a symlink; refusing to use it")
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


_lock_depth = threading.local()


@contextmanager
def line_lock(d: Path):
    """Exclusive lock for everything under ~/.ai-phone/lines (and the config/secrets writes that go with it).
    Re-entrant within a thread, so a whole CLI command can hold it while the helpers it calls take it again."""
    depth = getattr(_lock_depth, "n", 0)
    if depth:
        _lock_depth.n = depth + 1
        try:
            yield
        finally:
            _lock_depth.n -= 1
        return
    d = _private_dir(Path(d))
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)  # never follows a planted symlink; 0600
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        _lock_depth.n = 1
        try:
            yield
        finally:
            _lock_depth.n = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class Journal:
    def __init__(self, path: Path, doc: dict):
        self.path, self.doc = path, doc

    @classmethod
    def open(cls, d: Path, line_id: str, provider: str | None = None) -> "Journal":
        if not LINE_ID_RE.fullmatch(line_id):
            raise ValueError(f"line id {line_id!r} must match {LINE_ID_RE.pattern}")
        d = _private_dir(Path(d))
        path = d / f"{line_id}.journal.json"
        if path.is_symlink():
            raise RuntimeError(f"{path} is a symlink; refusing to use it")
        if path.exists():
            doc = json.loads(path.read_text())
            if provider and doc.get("provider") != provider:
                raise RuntimeError(f"journal for line {line_id} belongs to provider {doc.get('provider')}, not {provider}")
        else:
            doc = {"line_id": line_id, "provider": provider, "state": "pending", "steps": []}
        j = cls(path, doc)
        j._save()
        return j

    @property
    def state(self) -> str:
        return self.doc["state"]

    def set_state(self, state: Literal["pending", "ready", "failed", "rollback_failed"]) -> None:
        if state not in STATES:
            raise ValueError(f"journal state must be one of {STATES}, got {state!r}")
        self.doc["state"] = state
        self._save()

    def _step(self, step: str) -> dict | None:
        return next((s for s in self.doc["steps"] if s["name"] == step), None)

    def start(self, step: str, **planned) -> tuple[str, dict]:
        """('done', result) if the step already completed; ('started', planned) if it was begun but never finished
        (reconcile before creating anything); ('new', planned) after recording it as started."""
        s = self._step(step)
        if s is not None and s["status"] == "done":
            return "done", dict(s["result"])
        if s is not None:
            return "started", dict(s["planned"])
        self.doc["steps"].append({"name": step, "status": "started", "planned": planned, "result": {}, "at": time.time()})
        self._save()
        return "new", dict(planned)

    def finish(self, step: str, **result) -> None:
        s = self._step(step)
        if s is None:
            raise RuntimeError(f"finish({step!r}) before start")
        s.update(status="done", result=result, at=time.time())
        self._save()

    def steps_done(self) -> list[str]:
        return [s["name"] for s in self.doc["steps"] if s["status"] == "done"]

    def resources(self) -> dict:
        out: dict = {}
        for s in self.doc["steps"]:
            if s["status"] == "done":
                out.update(s["result"])
        return out

    def delete(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".journal-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.doc, f, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise
