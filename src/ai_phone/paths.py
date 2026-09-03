"""Every path under the ai-phone home directory. AI_PHONE_HOME relocates it (tests, multiple daemons)."""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("AI_PHONE_HOME") or "~/.ai-phone").expanduser()


def config_path() -> Path:
    return home() / "config.yaml"


def secrets_path() -> Path:
    return home() / "secrets.env"


def state_path() -> Path:
    return home() / "state.json"


def socket_path() -> Path:
    return home() / "control.sock"


def lines_dir() -> Path:
    return home() / "lines"


def logs_dir(cfg_log_dir: str) -> Path:
    return Path(cfg_log_dir).expanduser()
