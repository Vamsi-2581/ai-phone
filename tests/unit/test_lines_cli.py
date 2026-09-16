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
    (tmp_path / "config.yaml").write_text("inbound:\n  greeting: Hi\n")
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


def test_remove_refuses_to_release_without_the_journal(home: Path, capsys):
    """A lost journal releases nothing at the carrier, so removing the line would delete the only local record
    of resources that are still billing. Refuse, name the ids from the config, and keep everything local."""
    main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    (home / "lines" / "main.journal.json").unlink()
    assert main(["lines", "remove", "main", "--key-env", "FAKE_KEY", "--yes"]) == 3
    out = capsys.readouterr().out
    assert "journal" in out and "number_id=number-1" in out and "--local-only" in out
    assert "main" in load_config(home / "config.yaml").lines
    assert "AI_PHONE_LINE_MAIN_PASSWORD" in load_secrets(home / "secrets.env")
    assert json.loads((home / "fake-carrier.json").read_text())["carrier"]["number"]["name"] == "+442079460000"
    assert main(["lines", "remove", "main", "--local-only", "--yes"]) == 0  # the way out, with the ids to release by hand
    assert "number_id=number-1" in capsys.readouterr().out


def test_remove_refuses_when_the_journal_lost_its_steps(home: Path, capsys):
    """Same defect from the other direction: a journal that no longer accounts for the ids the config recorded."""
    main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    j = home / "lines" / "main.journal.json"
    j.write_text(json.dumps({**json.loads(j.read_text()), "steps": []}))
    assert main(["lines", "remove", "main", "--key-env", "FAKE_KEY", "--yes"]) == 3
    assert "number_id=number-1" in capsys.readouterr().out
    assert "main" in load_config(home / "config.yaml").lines


def test_remove_refuses_after_a_failed_resume_recreated_the_journal(home: Path, capsys):
    """`Journal.open` writes the file it invents, so a failed `lines resume` leaves an empty journal behind:
    the file existing must not be read as 'a release here would delete something'."""
    main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    (home / "lines" / "main.journal.json").unlink()
    assert main(["lines", "resume", "main", "--key-env", "FAKE_KEY"]) == 3
    assert (home / "lines" / "main.journal.json").exists()  # invented by the failed resume, no steps in it
    assert main(["lines", "remove", "main", "--key-env", "FAKE_KEY", "--yes"]) == 3
    assert "records no carrier steps" in capsys.readouterr().out
    assert "main" in load_config(home / "config.yaml").lines


def test_remove_refuses_when_neither_journal_nor_config_kept_the_ids(home: Path, capsys):
    """The narrow case where nothing local knows the ids: still refuse, because the carrier may hold them."""
    from ai_phone.lines.config_writer import read_raw, write_raw
    main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"])
    raw = read_raw(home / "config.yaml")
    raw["lines"]["main"]["provider"]["resources"] = {}  # a hand-edited config, or `_commit`'s failed-state fallback
    write_raw(home / "config.yaml", raw)
    j = home / "lines" / "main.journal.json"
    j.write_text(json.dumps({**json.loads(j.read_text()), "steps": []}))
    assert main(["lines", "remove", "main", "--key-env", "FAKE_KEY", "--yes"]) == 3
    assert "find them by +442079460000" in capsys.readouterr().out
    assert "main" in load_config(home / "config.yaml").lines
    assert json.loads((home / "fake-carrier.json").read_text())["carrier"]["number"]["name"] == "+442079460000"


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


def test_persona_option_is_gone(home: Path):
    with pytest.raises(SystemExit) as e:
        main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--persona", "x", "--yes"])
    assert e.value.code == 2


def test_added_lines_carry_no_persona(home: Path):
    from ai_phone.lines.config_writer import read_raw
    assert main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"]) == 0
    assert "persona" not in read_raw(home / "config.yaml")["lines"]["main"]


def test_unloadable_result_restores_the_config(home: Path, monkeypatch):
    from ai_phone.providers.base import ProvisionResult
    monkeypatch.setattr("ai_phone.providers.fake.FakeProvider.provision",
                        lambda self, journal, number, line_id, user, pw: ProvisionResult("ready", "ok", {"kind": "trunk", "registrar": "not a uri", "dial_domain": "x", "user": user}))
    from ai_phone.lines.config_writer import read_raw
    before = read_raw(home / "config.yaml")
    assert main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"]) == 3
    after = read_raw(home / "config.yaml")
    assert (after.get("lines") or {}) == {} and after["inbound"] == before["inbound"]  # restored (an empty lines: {} is equivalent)
    assert (home / "lines" / "main.journal.json").exists()  # the carrier side is kept for resume/remove


def test_failed_order_marks_an_existing_pending_line(home: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_FAKE_PENDING", "5")
    assert main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"]) == 2
    from ai_phone.providers.base import ProvisionResult
    monkeypatch.setattr("ai_phone.providers.fake.FakeProvider.provision",
                        lambda self, journal, number, line_id, user, pw: ProvisionResult("failed", "the carrier rejected the order"))
    assert main(["lines", "resume", "main", "--key-env", "FAKE_KEY"]) == 3
    assert load_config(home / "config.yaml").lines["main"].provider.state == "failed"


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


def test_resume_works_for_a_journal_without_the_target_step(home: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_FAKE_CRASH_AFTER", "number")
    assert main(["lines", "add", "fake", "--key-env", "FAKE_KEY", "--id", "main", "--yes"]) == 3
    j = Journal.open(home / "lines", "main")
    j.doc["steps"] = [s for s in j.doc["steps"] if s["name"] != "target"]  # a journal from before the target step existed
    j._save()
    monkeypatch.delenv("AI_PHONE_FAKE_CRASH_AFTER")
    assert main(["lines", "resume", "main", "--key-env", "FAKE_KEY"]) == 0
    assert load_config(home / "config.yaml").lines["main"].number == "+442079460000"


def test_pick_number_stops_cleanly_on_a_non_numeric_answer(monkeypatch, capsys):
    from types import SimpleNamespace

    from ai_phone import cli_lines

    class Provider:
        name = "fake"

        def search_numbers(self, country, area, limit):
            return [SimpleNamespace(e164="+15550000001", monthly_cost=1.0, currency="USD", requires_regulatory=False)]

    args = SimpleNamespace(number=None, country="US", area=None, yes=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    with pytest.raises(SystemExit):
        cli_lines._pick_number(Provider(), args)
    assert "stopped; nothing was bought" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt="": "7")
    with pytest.raises(SystemExit):
        cli_lines._pick_number(Provider(), args)
    assert "from 1 to 1" in capsys.readouterr().out


def test_bare_system_exit_message_is_printed_not_raised(home: Path, capsys, monkeypatch):
    """`--key-env` names a variable that is not set: one line on stderr and exit 2, never a traceback."""
    monkeypatch.delenv("NOPE_KEY", raising=False)
    rc = main(["lines", "--config", str(home / "config.yaml"), "add", "telnyx", "--key-env", "NOPE_KEY", "--yes"])
    assert rc == 2
    assert "NOPE_KEY" in capsys.readouterr().err


def test_missing_config_is_one_line(home: Path, capsys):
    rc = main(["lines", "--config", str(home / "absent.yaml"), "list"])
    assert rc != 0
    out = capsys.readouterr()
    assert "does not exist" in out.out + out.err
