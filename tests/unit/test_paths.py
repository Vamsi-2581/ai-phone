from pathlib import Path

from ai_phone import paths


def test_home_honours_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    assert paths.home() == tmp_path
    assert paths.config_path() == tmp_path / "config.yaml"
    assert paths.secrets_path() == tmp_path / "secrets.env"
    assert paths.state_path() == tmp_path / "state.json"
    sock = paths.socket_path()  # falls back to a short /tmp path when the home is too deep for AF_UNIX
    short_tmp = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")  # the same rule production uses
    if len(str(tmp_path / "control.sock").encode()) <= 100:
        assert sock == tmp_path / "control.sock"
    else:
        assert sock.parent.parent == short_tmp and sock.parent.name.startswith("ai-phone-")
    assert paths.lines_dir() == tmp_path / "lines"


def test_home_defaults_to_dot_ai_phone(monkeypatch):
    monkeypatch.delenv("AI_PHONE_HOME", raising=False)
    assert paths.home() == Path("~/.ai-phone").expanduser()


def test_socket_path_falls_back_to_a_short_tmp_path(monkeypatch, tmp_path: Path):
    deep = tmp_path / ("x" * 60) / ("y" * 60)
    monkeypatch.setenv("AI_PHONE_HOME", str(deep))
    sock = paths.socket_path()
    import os, stat
    assert len(str(sock).encode()) <= 100 and sock.suffix == ".sock" and sock.parent.name == f"ai-phone-{os.getuid()}"
    assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700 and sock.parent.stat().st_uid == os.getuid()
    assert paths.socket_path() == sock  # deterministic for the same home
    short = tmp_path / "short"
    monkeypatch.setenv("AI_PHONE_HOME", str(short))
    if len(str(short / "control.sock").encode()) <= 100:
        assert paths.socket_path() == short / "control.sock"
    else:  # macOS tmp paths are long: the private fallback applies, with the shape checked above
        assert paths.socket_path().suffix == ".sock" and paths.socket_path().parent.name == f"ai-phone-{os.getuid()}"


def test_call_counter_lives_in_the_home(tmp_path, monkeypatch):
    from ai_phone import paths
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    assert paths.call_counter_path() == tmp_path / "calls-today.json"
