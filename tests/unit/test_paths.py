from pathlib import Path

from ai_phone import paths


def test_home_honours_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    assert paths.home() == tmp_path
    assert paths.config_path() == tmp_path / "config.yaml"
    assert paths.secrets_path() == tmp_path / "secrets.env"
    assert paths.state_path() == tmp_path / "state.json"
    assert paths.socket_path() == tmp_path / "control.sock"
    assert paths.lines_dir() == tmp_path / "lines"


def test_home_defaults_to_dot_ai_phone(monkeypatch):
    monkeypatch.delenv("AI_PHONE_HOME", raising=False)
    assert paths.home() == Path("~/.ai-phone").expanduser()
