from pathlib import Path

from ai_phone import paths


def test_home_honours_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    assert paths.home() == tmp_path
    assert paths.config_path() == tmp_path / "config.yaml"
    assert paths.secrets_path() == tmp_path / "secrets.env"
    assert paths.state_path() == tmp_path / "state.json"
    sock = paths.socket_path()  # falls back to a short /tmp path when the home is too deep for AF_UNIX
    assert sock == tmp_path / "control.sock" if len(str(tmp_path / "control.sock").encode()) <= 100 else sock.parent == Path("/private/tmp")
    assert paths.lines_dir() == tmp_path / "lines"


def test_home_defaults_to_dot_ai_phone(monkeypatch):
    monkeypatch.delenv("AI_PHONE_HOME", raising=False)
    assert paths.home() == Path("~/.ai-phone").expanduser()


def test_socket_path_falls_back_to_a_short_tmp_path(monkeypatch, tmp_path: Path):
    deep = tmp_path / ("x" * 60) / ("y" * 60)
    monkeypatch.setenv("AI_PHONE_HOME", str(deep))
    sock = paths.socket_path()
    assert len(str(sock).encode()) <= 100 and sock.name.startswith("ai-phone-") and sock.suffix == ".sock"
    assert paths.socket_path() == sock  # deterministic for the same home
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path / "short"))
    assert paths.socket_path() == tmp_path / "short" / "control.sock" or len(str(tmp_path / "short")) > 87
