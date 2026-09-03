import json
from pathlib import Path

import pytest

from ai_phone.cli import main
from ai_phone.config import load_config
from ai_phone.lines.journal import Journal
from ai_phone.secrets import load_secrets


@pytest.fixture
def home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_KEY", "k")
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    (tmp_path / "config.yaml").write_text("personas:\n  default:\n    greeting: Hi\n")
    return tmp_path


def test_add_fake_provider_provisions_writes_line_and_secret(home: Path, capsys):
    rc = main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    assert rc == 0
    cfg = load_config(home / "config.yaml")
    ln = cfg.lines["main"]
    assert ln.kind == "trunk" and ln.registrar == "sip:sip.fake.invalid" and ln.provider.name == "fake" and ln.provider.state == "ready"
    assert ln.password_env == "AI_PHONE_LINE_MAIN_PASSWORD"
    pw = load_secrets(home / "secrets.env")["AI_PHONE_LINE_MAIN_PASSWORD"]
    assert len(pw) == 24
    assert Journal.open(home / "lines", "main").state == "ready"
    out = capsys.readouterr().out
    assert "+442079460000" in out and "ai-phone run" in out and pw not in out


def test_add_refuses_duplicate_id(home: Path):
    assert main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"]) == 0
    assert main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"]) == 3


def test_pending_then_resume(home: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_FAKE_PENDING", "2")
    assert main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"]) == 2
    assert load_config(home / "config.yaml").lines["main"].provider.state == "pending"
    monkeypatch.setenv("AI_PHONE_FAKE_PENDING", "0")
    assert main(["lines", "resume", "main", "--key-env", "FAKE_KEY"]) == 0
    assert load_config(home / "config.yaml").lines["main"].provider.state == "ready"


def test_remove_releases_and_deletes_secret(home: Path):
    main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    assert main(["lines", "remove", "main", "--key-env", "FAKE_KEY", "--yes"]) == 0
    assert load_config(home / "config.yaml").lines == {}
    assert "AI_PHONE_LINE_MAIN_PASSWORD" not in load_secrets(home / "secrets.env")
    assert not (home / "lines" / "main.journal.json").exists()


def test_remove_without_key_needs_local_only(home: Path, capsys):
    main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    assert main(["lines", "remove", "main", "--yes"]) == 3
    assert "--local-only" in capsys.readouterr().out
    assert main(["lines", "remove", "main", "--local-only", "--yes"]) == 0


def test_add_trunk_and_sip_by_hand(home: Path):
    assert main(["lines", "add", "trunk", "--id", "byo", "--number", "+442079460001", "--registrar", "sip:sip.example.com",
                 "--user", "byouser1", "--password-env", "BYO_PW", "--trusted-cidr", "198.51.100.0/24"]) == 0
    assert main(["lines", "add", "sip", "--id", "agents", "--user", "agents", "--host", "home.example.org", "--trusted", "203.0.113.7"]) == 0
    cfg = load_config(home / "config.yaml")
    assert cfg.lines["byo"].dial_domain == "sip.example.com" and cfg.lines["byo"].caller_id == "+442079460001"
    assert cfg.lines["agents"].aor == "sip:agents@home.example.org" and cfg.lines["agents"].accept_invites_from == "trusted"


def test_forward_prints_codes_and_records_divert(home: Path, capsys):
    main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    assert main(["lines", "forward", "main", "--when", "no-answer", "--from", "+447700900123"]) == 0
    out = capsys.readouterr().out
    assert "**61*+442079460000#" in out and "*#61#" in out and "##002#" in out
    assert load_config(home / "config.yaml").lines["main"].diverted_from == ["+447700900123"]


def test_verify_caller_id_records_it(home: Path, monkeypatch):
    main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    monkeypatch.setattr("builtins.input", lambda *a: "123456")
    assert main(["lines", "verify-caller-id", "main", "+447700900123", "--key-env", "FAKE_KEY"]) == 0
    v = load_config(home / "config.yaml").lines["main"].verified_caller_ids
    assert v and v[0].number == "+447700900123" and v[0].provider == "fake"


def test_list(home: Path, capsys):
    main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    assert main(["lines", "list"]) == 0
    assert "main" in capsys.readouterr().out


def test_manual_duplicate_id_keeps_the_existing_line(home: Path, capsys):
    main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    assert main(["lines", "add", "sip", "--id", "main", "--user", "agents", "--host", "home.example.org"]) == 3
    cfg = load_config(home / "config.yaml")
    assert cfg.lines["main"].kind == "trunk" and cfg.lines["main"].provider.name == "fake"


def test_manual_trunk_without_registrar_is_a_clean_error(home: Path, capsys):
    assert main(["lines", "add", "trunk", "--id", "byo", "--number", "+442079460001", "--user", "byouser1"]) == 3
    assert "--registrar" in capsys.readouterr().out


def test_resume_after_a_crash_mid_provision(home: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_FAKE_CRASH_AFTER", "connection")
    assert main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"]) == 3
    assert "main" not in (load_config(home / "config.yaml").lines)
    monkeypatch.delenv("AI_PHONE_FAKE_CRASH_AFTER")
    assert main(["lines", "resume", "main", "--key-env", "FAKE_KEY"]) == 0
    ln = load_config(home / "config.yaml").lines["main"]
    assert ln.provider.state == "ready" and ln.number == "+442079460000"
    j = Journal.open(home / "lines", "main")
    assert "carrier_ready" in j.steps_done() and "local_committed" in j.steps_done()


def test_provider_errors_exit_3_not_traceback(home: Path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_FAKE_CRASH_AFTER", "profile")
    assert main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"]) == 3
    assert "error:" in capsys.readouterr().out
