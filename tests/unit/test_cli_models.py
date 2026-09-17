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
