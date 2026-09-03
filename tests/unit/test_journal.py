import json
import stat
from pathlib import Path

import pytest

from ai_phone.lines.credentials import generate_sip_password, generate_sip_user
from ai_phone.lines.journal import Journal, line_lock


def test_journal_lifecycle(tmp_path: Path):
    j = Journal.open(tmp_path / "lines", "main", "telnyx")
    assert j.state == "pending" and stat.S_IMODE(j.path.parent.stat().st_mode) == 0o700
    status, planned = j.start("connection", name="ai-phone:main")
    assert status == "new" and planned == {"name": "ai-phone:main"}
    # crash here: re-open sees the step as started and hands back what was planned
    j2 = Journal.open(tmp_path / "lines", "main")
    assert j2.start("connection", name="ai-phone:main") == ("started", {"name": "ai-phone:main"})
    j2.finish("connection", connection_id="c-1")
    assert j2.start("connection") == ("done", {"connection_id": "c-1"})
    assert j2.resources() == {"connection_id": "c-1"} and j2.steps_done() == ["connection"]
    j2.set_state("ready")
    assert Journal.open(tmp_path / "lines", "main").state == "ready"
    assert stat.S_IMODE(j2.path.stat().st_mode) == 0o600
    j2.delete()
    assert not j2.path.exists()


def test_journal_refuses_symlinked_dir(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "lines").symlink_to(real)
    with pytest.raises(RuntimeError, match="symlink"):
        Journal.open(tmp_path / "lines", "main")


def test_lock_is_exclusive(tmp_path: Path):
    import fcntl
    with line_lock(tmp_path / "lines"):
        f = open(tmp_path / "lines" / ".lock", "w")
        with pytest.raises(BlockingIOError):
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_credentials_follow_carrier_rules():
    u = generate_sip_user("my-line-2")
    assert u.isalnum() and 4 <= len(u) <= 32 and u.startswith("aimyline2")
    for _ in range(50):
        p = generate_sip_password()
        assert len(p) == 24 and any(c.isdigit() for c in p) and any(c.isupper() for c in p) and any(c.islower() for c in p)


def test_open_rejects_path_traversal_and_bad_ids(tmp_path: Path):
    for bad in ("../outside", "Main", "a b", "x" * 33, ""):
        with pytest.raises(ValueError):
            Journal.open(tmp_path / "lines", bad)
    assert not (tmp_path / "outside.journal.json").exists()


def test_set_state_is_validated(tmp_path: Path):
    j = Journal.open(tmp_path / "lines", "main")
    with pytest.raises(ValueError):
        j.set_state("done")


def test_lock_file_is_private_and_never_a_symlink(tmp_path: Path):
    import os, stat
    d = tmp_path / "lines"
    d.mkdir(mode=0o700)
    target = tmp_path / "victim"
    target.write_text("keep me")
    (d / ".lock").symlink_to(target)
    with pytest.raises(OSError):
        with line_lock(d):
            pass
    assert target.read_text() == "keep me"
    (d / ".lock").unlink()
    with line_lock(d):
        pass
    assert stat.S_IMODE((d / ".lock").stat().st_mode) == 0o600
