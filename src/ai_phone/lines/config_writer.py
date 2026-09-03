"""Edit config.yaml one line at a time: read, change, write atomically (0600), under the lines lock."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from ai_phone import paths
from ai_phone.lines.journal import line_lock


def read_raw(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def write_raw(path: Path, raw: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(raw, f, sort_keys=False, default_flow_style=False)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def add_line(path: Path, line_id: str, line: dict) -> None:
    with line_lock(paths.lines_dir()):
        raw = read_raw(path)
        lines = raw.setdefault("lines", {}) or {}
        if line_id in lines:
            raise ValueError(f"line {line_id!r} already exists")
        lines[line_id] = line
        raw["lines"] = lines
        write_raw(path, raw)


def update_line(path: Path, line_id: str, **fields) -> None:
    with line_lock(paths.lines_dir()):
        raw = read_raw(path)
        lines = raw.get("lines") or {}
        if line_id not in lines:
            raise ValueError(f"no line {line_id!r}")
        lines[line_id].update(fields)
        write_raw(path, raw)


def remove_line(path: Path, line_id: str) -> None:
    with line_lock(paths.lines_dir()):
        raw = read_raw(path)
        lines = raw.get("lines") or {}
        lines.pop(line_id, None)
        raw["lines"] = lines
        write_raw(path, raw)
