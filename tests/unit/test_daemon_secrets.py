import os

from ai_phone.config import BrainConfig, Config, LineConfig
from ai_phone.daemon import Daemon


def test_daemon_moves_exported_secrets_out_of_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    monkeypatch.setenv("AI_PHONE_LINE_MAIN_PASSWORD", "pw-from-shell")
    monkeypatch.setenv("BRAIN_KEY", "sk-from-shell")
    cfg = Config(lines={"main": LineConfig(kind="trunk", number="+442079460000", registrar="sip:sip.telnyx.com", dial_domain="sip.telnyx.com",
                                            user="aimain1234", trusted_cidrs=["192.0.2.0/24"])},
                 brain=BrainConfig(kind="openai_compatible", base_url="http://127.0.0.1:1234/v1", api_key_env="BRAIN_KEY"))
    d = Daemon(cfg, "t4", tmp_path, lan=False, fake=True)
    assert d.secrets["AI_PHONE_LINE_MAIN_PASSWORD"] == "pw-from-shell" and d.secrets["BRAIN_KEY"] == "sk-from-shell"
    assert "AI_PHONE_LINE_MAIN_PASSWORD" not in os.environ and "BRAIN_KEY" not in os.environ
