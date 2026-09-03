import os
import stat
from pathlib import Path

import pytest

from ai_phone.secrets import SecretsError, load_secrets, redact, remove_secret, write_secret


def test_write_creates_private_file_and_dir(tmp_path: Path):
    p = tmp_path / "home" / "secrets.env"
    write_secret("AI_PHONE_LINE_MAIN_PASSWORD", "s3cret", p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert load_secrets(p) == {"AI_PHONE_LINE_MAIN_PASSWORD": "s3cret"}


def test_write_is_an_upsert_and_remove_deletes(tmp_path: Path):
    p = tmp_path / "secrets.env"
    write_secret("A", "1", p)
    write_secret("B", "2", p)
    write_secret("A", "3", p)
    assert load_secrets(p) == {"A": "3", "B": "2"}
    remove_secret("A", p)
    assert load_secrets(p) == {"B": "2"}


def test_loose_permissions_are_refused(tmp_path: Path):
    p = tmp_path / "secrets.env"
    write_secret("A", "1", p)
    os.chmod(p, 0o644)
    with pytest.raises(SecretsError, match="0600"):
        load_secrets(p)


def test_symlink_is_refused(tmp_path: Path):
    real = tmp_path / "real.env"
    write_secret("A", "1", real)
    link = tmp_path / "secrets.env"
    link.symlink_to(real)
    with pytest.raises(SecretsError, match="symlink"):
        load_secrets(link)
    with pytest.raises(SecretsError, match="symlink"):
        write_secret("B", "2", link)


def test_missing_file_is_empty(tmp_path: Path):
    assert load_secrets(tmp_path / "none.env") == {}


def test_bad_key_name_is_refused(tmp_path: Path):
    with pytest.raises(SecretsError, match="name"):
        write_secret("bad-name", "x", tmp_path / "s.env")


def test_redact_hides_values_and_bearer_tokens():
    text = "Authorization: Bearer abc.def password=hunter2 ok"
    assert redact(text, ["hunter2"]) == "Authorization: Bearer *** password=*** ok"


def test_symlinked_directory_is_refused(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "home"
    link.symlink_to(real)
    with pytest.raises(SecretsError, match="symlink"):
        write_secret("A", "1", link / "secrets.env")


def test_values_keep_their_spaces(tmp_path: Path):
    p = tmp_path / "secrets.env"
    write_secret("A", " padded value ", p)
    assert load_secrets(p) == {"A": " padded value "}


def test_symlinked_ancestor_is_refused(tmp_path: Path):
    real = tmp_path / "real"
    (real / "deeper").mkdir(parents=True)
    link = tmp_path / "home"
    link.symlink_to(real)
    with pytest.raises(SecretsError, match="symlink"):
        write_secret("A", "1", link / "deeper" / "secrets.env")
    with pytest.raises(SecretsError, match="symlink"):
        load_secrets(link / "deeper" / "secrets.env")
