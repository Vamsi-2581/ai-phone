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
    """The control socket. AF_UNIX paths are capped (104 bytes on macOS, 108 on Linux), so a home directory that
    is too deep gets a short, deterministic socket under /tmp instead — both the daemon and the CLI derive it here."""
    candidate = home() / "control.sock"
    if len(str(candidate).encode()) <= 100:
        return candidate
    import hashlib
    import stat
    digest = hashlib.sha256(str(home()).encode()).hexdigest()[:12]
    base = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")
    private = base / f"ai-phone-{os.getuid()}"  # a 0700 directory of our own inside the world-writable tmp
    if private.is_symlink():
        raise RuntimeError(f"{private} is a symlink; refusing to place the control socket there")
    private.mkdir(mode=0o700, exist_ok=True)
    st = private.stat()
    if st.st_uid != os.getuid():
        raise RuntimeError(f"{private} is not owned by this user; refusing to place the control socket there")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(private, 0o700)
    return private / f"{digest}.sock"


def lines_dir() -> Path:
    return home() / "lines"


def logs_dir(cfg_log_dir: str) -> Path:
    return Path(cfg_log_dir).expanduser()
