from pathlib import Path

import pytest

from ai_phone.config import ConfigError, LineConfig, load_config, validate_config

V2 = """
sip:
  bind: {address: 0.0.0.0, udp_port: 5060, tcp_port: 5060, tls_port: 5061}
lines:
  main:
    kind: trunk
    number: "+442079460000"
    display_name: "Sam's assistant"
    registrar: "sip:sip.telnyx.com"
    dial_domain: "sip.telnyx.com"
    user: "aimain7f3a"
    password_env: AI_PHONE_LINE_MAIN_PASSWORD
    accept_invites_from: registered_trunk
    trusted_cidrs: ["192.76.120.0/24"]
    caller_id: "+442079460000"
  agents:
    kind: sip
    user: "agents"
    aor: "sip:agents@home.example.org"
    accept_invites_from: trusted
    trusted_peers: ["203.0.113.7"]
    caller_id: "sip:agents@home.example.org"
personas:
  default:
    greeting: "Hi."
    system_prompt: "s"
    outbound_opening: "Hi, this is an AI assistant."
"""

LEGACY = """
sip:
  user: "aiuser"
  domain: "sip.example.com"
  password_env: SIP_PW
  accept_invites_from: registered_trunk
  bind: {addr: 0.0.0.0, port: 5070}
conversation:
  greeting: "Hello there."
  system_prompt: "You are ai-phone."
  endpoint_silence_ms: 250
"""


def _load(tmp_path: Path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return load_config(p)


def test_v2_config_loads(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_LINE_MAIN_PASSWORD", "x")
    cfg = _load(tmp_path, V2)
    assert set(cfg.lines) == {"main", "agents"}
    assert cfg.lines["main"].kind == "trunk" and cfg.lines["main"].register_via_proxy is True
    assert cfg.lines["agents"].kind == "sip" and cfg.lines["agents"].trusted_peers == ["203.0.113.7"]
    assert cfg.personas["default"].outbound_opening.startswith("Hi, this is an AI")
    assert cfg.brain.kind == "local"
    assert cfg.sip.bind.tls_port == 5061 and cfg.sip.tcp_keepalive_sec == 30
    assert cfg.conversation.endpoint_silence_ms == 300
    assert cfg.warnings == []


def test_empty_config_has_no_lines_and_a_default_persona(tmp_path: Path):
    cfg = _load(tmp_path, "")
    assert cfg.lines == {}
    assert "default" in cfg.personas


def test_legacy_m1_config_is_migrated_with_a_warning(tmp_path: Path):
    cfg = _load(tmp_path, LEGACY)
    assert any("Milestone 1" in w for w in cfg.warnings)
    main = cfg.lines["main"]
    assert main.kind == "trunk" and main.registrar == "sip:sip.example.com" and main.dial_domain == "sip.example.com"
    assert main.user == "aiuser" and main.password_env == "SIP_PW"
    assert cfg.sip.bind.udp_port == 5070 and cfg.sip.bind.tcp_port == 5070
    assert cfg.personas["default"].greeting == "Hello there."
    assert cfg.conversation.endpoint_silence_ms == 250


def test_legacy_sip_only_config_becomes_a_sip_line(tmp_path: Path):
    cfg = _load(tmp_path, 'sip:\n  user: "ai"\n  accept_invites_from: lan\n')
    main = cfg.lines["main"]
    assert main.kind == "sip" and main.aor == "sip:ai@127.0.0.1" and main.accept_invites_from == "lan"


def test_inline_legacy_password_is_refused(tmp_path: Path):
    with pytest.raises(ConfigError, match="password_env"):
        _load(tmp_path, 'sip:\n  user: "ai"\n  domain: "x.example"\n  password: "nope"\n')


@pytest.mark.parametrize("patch, msg", [
    ("    user: \"ab\"\n", "user"),                                  # too short
    ("    display_name: 'has \"quote\"'\n", "display_name"),
    ("    registrar: \"sip.telnyx.com\"\n", "registrar"),             # not a sip: URI
    ("    dial_domain: \"sip:sip.telnyx.com\"\n", "dial_domain"),     # must be a hostname
    ("    caller_id: \"02079460000\"\n", "caller_id"),                # not E.164
    ("    srtp: required\n", "srtp"),                                  # needs tls
    ("    password_env: \"lower\"\n", "password_env"),
])
def test_grammar_errors_name_the_field(tmp_path: Path, patch: str, msg: str):
    bad = V2.replace('    caller_id: "+442079460000"\n', '    caller_id: "+442079460000"\n' + patch, 1)
    with pytest.raises(ConfigError, match=msg):
        _load(tmp_path, bad)


def test_trusted_mode_requires_peers(tmp_path: Path):
    bad = V2.replace('    trusted_peers: ["203.0.113.7"]\n', "")
    with pytest.raises(ConfigError, match="trusted_peers"):
        _load(tmp_path, bad)


def test_persona_reference_must_exist(tmp_path: Path):
    bad = V2.replace('    caller_id: "sip:agents@home.example.org"\n', '    caller_id: "sip:agents@home.example.org"\n    persona: nope\n')
    with pytest.raises(ConfigError, match="persona"):
        _load(tmp_path, bad)


def test_sip_users_must_be_unique_across_trunk_lines(tmp_path: Path):
    second = ("  second:\n    kind: trunk\n    number: \"+442079460001\"\n    registrar: \"sip:sip.telnyx.com\"\n    dial_domain: \"sip.telnyx.com\"\n"
              "    user: \"aimain7f3a\"\n    password_env: AI_PHONE_LINE_SECOND_PASSWORD\n    trusted_cidrs: [\"192.76.120.0/24\"]\n    caller_id: \"+442079460001\"\n")
    bad = V2.replace("personas:\n", second + "personas:\n")
    with pytest.raises(ConfigError, match="unique"):
        _load(tmp_path, bad)


def test_tcp_and_tls_ports_must_differ(tmp_path: Path):
    bad = V2.replace("tls_port: 5061", "tls_port: 5060")
    with pytest.raises(ConfigError, match="tls_port"):
        _load(tmp_path, bad)


def test_remote_brain_must_be_https(tmp_path: Path):
    bad = V2 + "brain:\n  kind: openai_compatible\n  base_url: \"http://brain.example.org/v1\"\n"
    with pytest.raises(ConfigError, match="https"):
        _load(tmp_path, bad)
    ok = V2 + "brain:\n  kind: openai_compatible\n  base_url: \"http://127.0.0.1:1234/v1\"\n"
    assert _load(tmp_path, ok).brain.base_url.endswith("/v1")


def test_missing_password_env_is_a_warning_not_an_error(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AI_PHONE_LINE_MAIN_PASSWORD", raising=False)
    cfg = _load(tmp_path, V2)
    warnings = validate_config(cfg, env={})
    assert any("AI_PHONE_LINE_MAIN_PASSWORD" in w for w in warnings)


def test_unknown_key_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="unknown key"):
        _load(tmp_path, V2 + "bogus: 1\n")


def test_brain_host_is_parsed_not_split(tmp_path: Path):
    sneaky = V2 + "brain:\n  kind: openai_compatible\n  base_url: \"http://127.0.0.1:80@brain.example.org/v1\"\n"
    with pytest.raises(ConfigError, match="https"):
        _load(tmp_path, sneaky)


def test_sips_scheme_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="registrar"):
        _load(tmp_path, V2.replace('registrar: "sip:sip.telnyx.com"', 'registrar: "sips:sip.telnyx.com"'))


def test_malformed_legacy_sections_are_config_errors(tmp_path: Path):
    with pytest.raises(ConfigError, match="config.sip"):
        _load(tmp_path, "sip: 5\n")
    with pytest.raises(ConfigError, match="bind.port"):
        _load(tmp_path, 'sip:\n  user: "ai"\n  bind: {port: "five"}\n')


def test_models_home_follows_ai_phone_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    assert _load(tmp_path, "").models.home == str(tmp_path / "models")


def test_trailing_newline_in_a_header_bound_field_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="display_name"):
        _load(tmp_path, V2.replace('display_name: "Sam\'s assistant"', 'display_name: "Sam\\n"'))
