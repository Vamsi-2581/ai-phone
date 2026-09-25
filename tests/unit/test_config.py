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
inbound:
  greeting: "Hi."
  instructions: "Take a message."
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
    assert cfg.inbound.greeting == "Hi." and cfg.inbound.instructions == "Take a message."
    assert cfg.calls.daily_limit == 0 and cfg.calls.relay_filler_after_ms == 1500
    assert cfg.brain.kind == "local"
    assert cfg.sip.bind.tls_port == 5061 and cfg.sip.tcp_keepalive_sec == 30
    assert cfg.conversation.endpoint_silence_ms == 300
    assert cfg.warnings == []


def test_empty_config_has_no_lines_and_a_default_greeting(tmp_path: Path):
    cfg = _load(tmp_path, "")
    assert cfg.lines == {}
    assert cfg.inbound.greeting.strip()


def test_legacy_m1_config_is_migrated_with_a_warning(tmp_path: Path):
    cfg = _load(tmp_path, LEGACY)
    assert any("Milestone 1" in w for w in cfg.warnings)
    main = cfg.lines["main"]
    assert main.kind == "trunk" and main.registrar == "sip:sip.example.com" and main.dial_domain == "sip.example.com"
    assert main.user == "aiuser" and main.password_env == "SIP_PW"
    assert cfg.sip.bind.udp_port == 5070 and cfg.sip.bind.tcp_port == 5070
    assert cfg.inbound.greeting == "Hello there." and cfg.inbound.instructions == "You are ai-phone."
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


def test_models_tier_takes_auto_a_catalog_key_or_a_repo(tmp_path: Path):
    for tier in ("auto", "t4", "gemma12b", "org/my-own-4bit"):
        assert _load(tmp_path, V2 + f"models:\n  tier: {tier}\n").models.tier == tier
    with pytest.raises(ConfigError) as bad:
        _load(tmp_path, V2 + "models:\n  tier: t99\n")
    assert "config.models.tier" in str(bad.value) and "gemma12b" in str(bad.value) and "org/name" in str(bad.value)


def test_trusted_mode_requires_peers(tmp_path: Path):
    bad = V2.replace('    trusted_peers: ["203.0.113.7"]\n', "")
    with pytest.raises(ConfigError, match="trusted_peers"):
        _load(tmp_path, bad)


def test_sip_users_must_be_unique_across_trunk_lines(tmp_path: Path):
    second = ("  second:\n    kind: trunk\n    number: \"+442079460001\"\n    registrar: \"sip:sip.telnyx.com\"\n    dial_domain: \"sip.telnyx.com\"\n"
              "    user: \"aimain7f3a\"\n    password_env: AI_PHONE_LINE_SECOND_PASSWORD\n    trusted_cidrs: [\"192.76.120.0/24\"]\n    caller_id: \"+442079460001\"\n")
    bad = V2.replace("inbound:\n", second + "inbound:\n")
    with pytest.raises(ConfigError, match="unique"):
        _load(tmp_path, bad)


PERSONAS = """
lines:
  agents:
    kind: sip
    user: "agents"
    aor: "sip:agents@home.example.org"
    accept_invites_from: lan
    persona: helper
personas:
  default:
    greeting: "Default hi."
    system_prompt: "Default prompt."
  helper:
    greeting: "Helper hi."
    system_prompt: "Helper prompt."
    outbound_opening: "Hello from helper."
"""


def test_personas_are_converted_to_inbound_with_one_warning(tmp_path: Path):
    cfg = _load(tmp_path, PERSONAS)
    assert cfg.inbound.greeting == "Helper hi." and cfg.inbound.instructions == "Helper prompt."
    w = [x for x in cfg.warnings if "personas" in x]
    assert len(w) == 1 and "default" in w[0] and "outbound_opening" in w[0]
    assert not hasattr(cfg.lines["agents"], "persona")


def test_explicit_inbound_wins_over_personas(tmp_path: Path):
    cfg = _load(tmp_path, PERSONAS + 'inbound:\n  greeting: "Mine."\n')
    assert cfg.inbound.greeting == "Mine." and cfg.inbound.instructions == "Helper prompt."


def test_personas_must_be_a_mapping(tmp_path: Path):
    with pytest.raises(ConfigError, match="personas"):
        _load(tmp_path, "personas: [1, 2]\n")
    with pytest.raises(ConfigError, match="personas"):
        _load(tmp_path, "personas:\n  default: 5\n")
    assert _load(tmp_path, "personas:\n  default:\n").inbound.greeting  # an emptied persona loaded before and still does


@pytest.mark.parametrize("calls, match", [
    ("calls:\n  daily_limit: -1\n", "daily_limit"),
    ("calls:\n  relay_filler_after_ms: 0\n", "relay_filler_after_ms"),
    ("calls:\n  relay_filler_after_ms: 12000\n  relay_second_filler_sec: 10\n", "relay_second_filler_sec"),
    ("calls:\n  relay_second_filler_sec: 30\n  relay_reply_timeout_sec: 25\n", "relay_reply_timeout_sec"),
    ('inbound:\n  greeting: ""\n', "inbound.greeting"),
])
def test_calls_and_inbound_are_validated(tmp_path: Path, calls, match):
    with pytest.raises(ConfigError, match=match):
        _load(tmp_path, calls)


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


def test_password_in_secrets_env_is_not_warned_about(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AI_PHONE_LINE_MAIN_PASSWORD", raising=False)
    cfg = _load(tmp_path, V2)
    assert any("AI_PHONE_LINE_MAIN_PASSWORD" in w for w in cfg.warnings)
    cfg2 = load_config(tmp_path / "config.yaml", env={"AI_PHONE_LINE_MAIN_PASSWORD": "from-secrets-env"})
    assert not any("AI_PHONE_LINE_MAIN_PASSWORD" in w for w in cfg2.warnings)


def test_any_policy_is_warned_about(tmp_path: Path):
    cfg = _load(tmp_path, V2.replace("accept_invites_from: registered_trunk", "accept_invites_from: any"))
    assert any("'any'" in w for w in cfg.warnings)


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
    with pytest.raises(ConfigError, match="config.inbound"):
        _load(tmp_path, 'inbound: 5\nconversation:\n  greeting: "Hi."\n')
    with pytest.raises(ConfigError, match="config.inbound"):
        _load(tmp_path, "inbound: [1]\npersonas: {}\n")
    assert _load(tmp_path, PERSONAS.replace("persona: helper", "persona: [helper]")).inbound.greeting == "Default hi."


def test_models_home_follows_ai_phone_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    assert _load(tmp_path, "").models.home == str(tmp_path / "models")


def test_trailing_newline_in_a_header_bound_field_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="display_name"):
        _load(tmp_path, V2.replace('display_name: "Sam\'s assistant"', 'display_name: "Sam\\n"'))


def test_bad_provider_field_names_the_field(tmp_path: Path):
    bad = V2.replace('    caller_id: "+442079460000"\n', '    caller_id: "+442079460000"\n    provider: {name: telnyx, state: nope, resources: {}}\n', 1)
    with pytest.raises(ConfigError, match="provider.state"):
        _load(tmp_path, bad)


def test_bad_yaml_and_missing_file_are_config_errors(tmp_path):
    import pytest
    from ai_phone.config import ConfigError, load_config
    bad = tmp_path / "config.yaml"
    bad.write_text("lines: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)
    with pytest.raises(ConfigError, match="does not exist"):
        load_config(tmp_path / "nope.yaml")


def _installed(monkeypatch, *names):
    from ai_phone import voices
    monkeypatch.setattr(voices, "installed_voices", lambda root=None: list(names))


def test_default_voice_loads_without_looking_on_disk(tmp_path: Path, monkeypatch):
    from ai_phone import voices

    def boom(*a, **k):
        raise AssertionError("the default voice must not look for installed voices")
    monkeypatch.setattr(voices, "installed_voices", boom)
    monkeypatch.setattr(voices, "voices_dir", boom)
    cfg = _load(tmp_path, "")
    assert (cfg.tts.voice, cfg.tts.speed) == ("af_heart", 1.0)
    assert _load(tmp_path, "tts: {voice: af_heart, speed: 1.2}\n").tts.speed == 1.2
    assert cfg.warnings == []


def test_an_installed_voice_loads(tmp_path: Path, monkeypatch):
    _installed(monkeypatch, "af_heart", "bm_george")
    cfg = _load(tmp_path, "tts:\n  voice: bm_george\n  speed: 0.9\n")
    assert (cfg.tts.voice, cfg.tts.speed) == ("bm_george", 0.9)
    assert cfg.warnings == []


def test_a_voice_that_is_not_installed_lists_the_installed_ones(tmp_path: Path, monkeypatch):
    _installed(monkeypatch, "af_heart", "bm_george")
    with pytest.raises(ConfigError, match="config.tts.voice: bm_nobody is not installed; installed: af_heart, bm_george"):
        _load(tmp_path, "tts: {voice: bm_nobody}\n")


def test_voice_must_be_english(tmp_path: Path, monkeypatch):
    _installed(monkeypatch, "af_heart", "jf_alpha")
    with pytest.raises(ConfigError, match="config.tts.voice: 'jf_alpha' is not an English voice"):
        _load(tmp_path, "tts: {voice: jf_alpha}\n")


@pytest.mark.parametrize("speed", ["3", "0.4", "true"])
def test_speed_is_bounded(tmp_path: Path, speed):
    with pytest.raises(ConfigError, match="config.tts.speed"):
        _load(tmp_path, f"tts: {{speed: {speed}}}\n")


def test_voice_cannot_be_checked_before_the_model_is_downloaded(tmp_path: Path, monkeypatch):
    _installed(monkeypatch)
    cfg = _load(tmp_path, "tts: {voice: bf_emma}\n")
    assert cfg.tts.voice == "bf_emma"
    assert cfg.warnings == ["config.tts.voice: bf_emma cannot be checked until the speech model is downloaded (ai-phone models pull)"]
