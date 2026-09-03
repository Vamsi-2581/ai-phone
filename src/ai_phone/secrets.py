"""~/.ai-phone/secrets.env: generated SIP passwords, one KEY=VALUE per line, 0600, never reached through a symlink.

Every open walks the path one component at a time with O_NOFOLLOW (directory descriptors + openat), so no
component can be swapped for a symlink between a check and the open. The mode is verified on the descriptor.
"""
from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path

from ai_phone import paths

KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_BEARER = re.compile(r"(Bearer )\S+")
_PASSWORD = re.compile(r"(password=)\S+")
_DIR_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)


class SecretsError(RuntimeError):
    pass


def _open_dir_nofollow(directory: Path, create: bool) -> int:
    """Descriptor for `directory`, reached component by component without following symlinks.
    With create=True, missing components are created 0700 (and existing ones are set to 0700)."""
    parts = Path(os.path.abspath(directory)).parts
    fd = os.open(parts[0], _DIR_FLAGS)  # "/"
    try:
        for name in parts[1:]:
            try:
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o700, dir_fd=fd)
                nxt = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):  # a symlink (O_NOFOLLOW) or a non-directory in the path
                    raise SecretsError(f"{directory}: {name!r} is a symlink or not a directory; refusing to use it") from None
                raise
            os.close(fd)
            fd = nxt
        if create:
            os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_file(dirfd: int, name: str) -> str | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):  # O_NOFOLLOW on a symlink: ELOOP on Linux/macOS (EMLINK on some BSDs)
            raise SecretsError(f"{name} is a symlink; refusing to use it") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SecretsError(f"{name} is not a regular file")
        mode = stat.S_IMODE(st.st_mode)
        if mode != 0o600:
            raise SecretsError(f"{name} has mode {mode:o}; it must be 0600 (chmod 600 {name})")
        with os.fdopen(fd) as f:
            fd = -1
            return f.read()
    finally:
        if fd != -1:
            os.close(fd)


def _parse(text: str, label: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not KEY_RE.fullmatch(key):
            raise SecretsError(f"{label}:{n}: expected KEY=VALUE")
        out[key] = value  # values keep their spaces
    return out


def load_secrets(path: Path | None = None) -> dict[str, str]:
    path = Path(path) if path else paths.secrets_path()
    absolute = Path(os.path.abspath(path))
    try:
        dirfd = _open_dir_nofollow(absolute.parent, create=False)
    except FileNotFoundError:
        return {}
    try:
        text = _read_file(dirfd, absolute.name)
    finally:
        os.close(dirfd)
    return {} if text is None else _parse(text, str(path))


def _write_all(path: Path, values: dict[str, str]) -> None:
    absolute = Path(os.path.abspath(path))
    dirfd = _open_dir_nofollow(absolute.parent, create=True)
    try:
        for _ in range(8):  # a fresh random name each time; O_EXCL makes a leftover or a concurrent writer harmless
            tmp = f".{absolute.name}.{os.urandom(8).hex()}.tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
                break
            except FileExistsError:
                continue
        else:
            raise SecretsError(f"could not create a temporary file next to {path}")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("".join(f"{k}={v}\n" for k, v in sorted(values.items())))
            os.replace(tmp, absolute.name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dirfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dirfd)


def write_secret(key: str, value: str, path: Path | None = None) -> None:
    if not KEY_RE.fullmatch(key):
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
