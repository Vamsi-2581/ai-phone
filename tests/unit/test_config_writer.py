import stat
from pathlib import Path

import pytest
import yaml

from ai_phone.lines.config_writer import add_line, read_raw, remove_line, update_line


def test_add_update_remove_round_trip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("inbound:\n  greeting: Hi\n")
    add_line(cfg, "main", {"kind": "trunk", "number": "+442079460000", "registrar": "sip:sip.telnyx.com", "dial_domain": "sip.telnyx.com", "user": "aimain1234"})
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    raw = read_raw(cfg)
    assert raw["lines"]["main"]["number"] == "+442079460000" and raw["inbound"]["greeting"] == "Hi"
    update_line(cfg, "main", diverted_from=["+447700900123"])
    assert read_raw(cfg)["lines"]["main"]["diverted_from"] == ["+447700900123"]
    with pytest.raises(ValueError, match="exists"):
        add_line(cfg, "main", {})
    remove_line(cfg, "main")
    assert read_raw(cfg)["lines"] == {}
    assert not list(tmp_path.glob(".config-*"))


def test_writer_keeps_yaml_readable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    add_line(cfg, "agents", {"kind": "sip", "aor": "sip:agents@home.example.org", "user": "agents"})
    text = cfg.read_text()
    assert "lines:\n  agents:\n    kind: sip" in text and yaml.safe_load(text)["lines"]["agents"]["user"] == "agents"
