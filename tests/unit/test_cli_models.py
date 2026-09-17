import contextlib
import logging
import sys
import types

import pytest

from ai_phone import models
from ai_phone.cli import main


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    monkeypatch.setattr(models, "memory_gb", lambda: 24)
    return tmp_path


def test_pull_with_everything_present(home, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr(models, "missing", lambda tier, brain: asked.append((tier, brain)) or [])
    monkeypatch.setattr(models, "pull", lambda *a, **k: pytest.fail("nothing to download"))
    assert main(["models", "pull"]) == 0
    assert "All models are already downloaded." in capsys.readouterr().out
    assert asked == [("t4", "local")]  # no config: the default brain, and auto picks t4 on 24 GB


def test_pull_downloads_what_is_missing(home, monkeypatch, capsys):
    (home / "config.yaml").write_text("brain:\n  kind: openai_compatible\n  base_url: http://127.0.0.1:1234/v1\n")
    pulled, asked = [], []
    monkeypatch.setattr(models, "missing", lambda tier, brain: asked.append((tier, brain)) or [models.STT, models.TTS])
    monkeypatch.setattr(models, "download_size", lambda repos: 2_500_000_000)
    monkeypatch.setattr(models, "pull", lambda repos, report=None: pulled.append(repos))
    assert main(["models", "pull", "--tier", "t9"]) == 0
    out = capsys.readouterr().out
    assert "Downloading 2 model(s), 2.5 GB, into the Hugging Face cache:" in out
    assert models.STT in out and models.TTS in out and "Done." in out
    assert pulled == [[models.STT, models.TTS]]
    assert asked == [("t9", "openai_compatible")]


def test_pull_reports_a_download_error(home, monkeypatch, capsys):
    def fail(repos, report=None):
        raise models.ModelsError("not enough disk space: 5.0 GB to download and 1.0 GB free (keep 1 GB spare)")

    monkeypatch.setattr(models, "missing", lambda tier, brain: [models.TTS])
    monkeypatch.setattr(models, "download_size", lambda repos: None)
    monkeypatch.setattr(models, "pull", fail)
    assert main(["models", "pull"]) == 3
    out = capsys.readouterr().out
    assert "an unknown amount" in out and "error: not enough disk space" in out


def test_pull_works_with_a_config_that_does_not_load(home, monkeypatch, capsys):
    """The recovery command must not crash: a voice named before the speech model is complete makes the config fail."""
    from ai_phone import voices
    monkeypatch.setattr(voices, "installed_voices", lambda root=None: ["af_heart"])
    (home / "config.yaml").write_text("models: {tier: t9}\nbrain: {kind: openai_compatible, base_url: 'https://api.example.com/v1'}\n"
                                      "tts: {voice: bf_emma}\n")
    asked = []
    monkeypatch.setattr(models, "missing", lambda tier, brain: asked.append((tier, brain)) or [])
    assert main(["models", "pull"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == ("warning: the config does not load (config.tts.voice: bf_emma is not installed; installed: af_heart); "
                      "using models.tier t9 and brain.kind openai_compatible from it")
    assert asked == [("t9", "openai_compatible")]
    (home / "config.yaml").write_text("models: {tier: t99}\nbrain: [not: yaml\n")
    assert main(["models", "pull"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("warning: the config does not load (config: ") and out[0].endswith("); using the defaults")
    assert len([ln for ln in out if ln.startswith("warning")]) == 1
    assert asked[-1] == ("t4", "local")


def test_pull_stopped_with_ctrl_c_exits_at_once(home, monkeypatch, capsys):
    """huggingface_hub joins its download threads at exit, so a normal exit would wait for the whole download."""
    import os

    def interrupted(repos, report=None):
        raise KeyboardInterrupt

    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(models, "missing", lambda tier, brain: [models.TTS])
    monkeypatch.setattr(models, "download_size", lambda repos: None)
    monkeypatch.setattr(models, "pull", interrupted)
    monkeypatch.setattr(os, "_exit", fake_exit)
    assert main(["models", "pull"]) == 130
    assert exits == [130]
    assert capsys.readouterr().out.endswith("\nStopped. Run it again to continue where it left off.\n")


@pytest.mark.parametrize("argv, plain", [(["setup", "--plain"], True), (["setup"], None)])
def test_setup_calls_the_wizard(monkeypatch, argv, plain):
    seen = []

    def fake(plain):
        seen.append(plain)
        return 0

    monkeypatch.setitem(sys.modules, "ai_phone.setup", types.SimpleNamespace(run_setup=fake))
    assert main(argv) == 0
    assert seen == [plain]


class FakeDaemon:
    tiers: list[str] = []

    def __init__(self, cfg, tier, log_dir, lan, fake):
        FakeDaemon.tiers.append(tier)

    async def run(self):
        pass


@pytest.mark.parametrize("argv, config, tier", [
    (["run"], "", "t4"),                                   # models.tier: auto on 24 GB
    (["run"], "models:\n  tier: t9\n", "t9"),              # the config's tier
    (["run", "--tier", "t4"], "models:\n  tier: t9\n", "t4"),  # the flag wins
])
def test_run_follows_the_config_tier(home, monkeypatch, argv, config, tier):
    import asyncio

    import ai_phone.daemon

    (home / "config.yaml").write_text(config)
    FakeDaemon.tiers = []
    monkeypatch.setattr(ai_phone.daemon, "Daemon", FakeDaemon)
    monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())
    assert main(argv + ["--fake"]) == 0
    assert FakeDaemon.tiers == [tier]


@contextlib.contextmanager
def fresh_logging():
    """Logging as a new process has it (no root handlers, root at WARNING); pytest's own handlers come back after."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    try:
        yield root
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


class Seen(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def handled(root, logger, level) -> bool:
    seen = Seen()
    root.addHandler(seen)
    try:
        logging.getLogger(logger).log(level, 'HTTP Request: GET https://huggingface.co/api/models/x "HTTP/1.1 200 OK"')
    finally:
        root.removeHandler(seen)
    return bool(seen.records)


@pytest.mark.parametrize("argv", [["models", "pull"], ["setup", "--plain"]])
def test_request_lines_do_not_flood_the_screen(home, monkeypatch, argv):
    monkeypatch.setattr(models, "missing", lambda tier, brain: [])
    monkeypatch.setitem(sys.modules, "ai_phone.setup", types.SimpleNamespace(run_setup=lambda plain: 0))
    with fresh_logging() as root:
        assert main(argv) == 0
        assert not handled(root, "httpx", logging.INFO)  # one per Hub or carrier request
        assert handled(root, "httpx", logging.WARNING)
        assert root.handlers == []  # Python's own last-resort handler writes to the current stderr, which the wizard captures


def test_run_still_logs_at_info(home, monkeypatch):
    import asyncio

    import ai_phone.daemon

    (home / "config.yaml").write_text("")
    monkeypatch.setattr(ai_phone.daemon, "Daemon", FakeDaemon)
    monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())
    with fresh_logging() as root:
        assert main(["run", "--fake"]) == 0
        assert handled(root, "ai-phone", logging.INFO)
