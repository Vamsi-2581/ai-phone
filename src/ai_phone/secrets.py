"""~/.ai-phone/secrets.env: generated SIP passwords, one KEY=VALUE per line, 0600, never symlinked."""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

from ai_phone import paths

KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_BEARER = re.compile(r"(Bearer )\S+")
_PASSWORD = re.compile(r"(password=)\S+")


class SecretsError(RuntimeError):
    pass


def _check(path: Path) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise SecretsError(f"{path} (or its directory) is a symlink; refusing to use it")
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            raise SecretsError(f"{path} has mode {mode:o}; it must be 0600 (chmod 600 {path})")


def load_secrets(path: Path | None = None) -> dict[str, str]:
    path = Path(path) if path else paths.secrets_path()
    _check(path)
    if not path.exists():
        return {}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)  # no symlink swap between the check above and this read
    with os.fdopen(fd) as f:
        text = f.read()
    out: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not KEY_RE.match(key):
            raise SecretsError(f"{path}:{n}: expected KEY=VALUE")
        out[key] = value  # values keep their spaces
    return out


def _write_all(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("".join(f"{k}={v}\n" for k, v in sorted(values.items())))
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def write_secret(key: str, value: str, path: Path | None = None) -> None:
    if not KEY_RE.match(key):
        raise SecretsError(f"secret name {key!r} must match {KEY_RE.pattern}")
    if "\n" in value or "\r" in value:
        raise SecretsError("secret values cannot contain newlines")
    path = Path(path) if path else paths.secrets_path()
    values = load_secrets(path)
    values[key] = value
    _write_all(path, values)


def remove_secret(key: str, path: Path | None = None) -> None:
    path = Path(path) if path else paths.secrets_path()
    values = load_secrets(path)
    values.pop(key, None)
    _write_all(path, values)


def redact(text: str, values) -> str:
    for v in sorted((v for v in values if v), key=len, reverse=True):
        text = text.replace(v, "***")
    text = _BEARER.sub(r"\1***", text)
    return _PASSWORD.sub(r"\1***", text)
