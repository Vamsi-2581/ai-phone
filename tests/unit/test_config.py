from pathlib import Path

import pytest

from ai_phone.config import ConfigError, load_config

MINIMAL = """
sip:
  user: "ai"
  accept_invites_from: lan
conversation:
  greeting: "Hi, this is the house."
  system_prompt: "You are ai-phone."
"""


def test_minimal_config_loads_with_defaults(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL)
    cfg = load_config(p)
    assert cfg.sip.user == "ai"
    assert cfg.sip.accept_invites_from == "lan"
    assert cfg.sip.bind.port == 5060
    assert cfg.rtp.port_min == 10000 and cfg.rtp.port_max == 10100
    assert cfg.conversation.endpoint_silence_ms == 300
    assert cfg.conversation.bargein_min_speech_ms == 250
    assert cfg.conversation.history_turns == 6
    assert cfg.models.tier == "auto"
    assert cfg.codecs == ["PCMU", "PCMA"]


def test_unknown_key_is_an_error(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL + "\nbogus: 1\n")
    with pytest.raises(ConfigError, match="bogus"):
        load_config(p)


def test_bad_acceptance_mode_is_an_error(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(MINIMAL.replace("lan", "everyone"))
    with pytest.raises(ConfigError, match="accept_invites_from"):
        load_config(p)
