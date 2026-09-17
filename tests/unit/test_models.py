import inspect
import subprocess
import types

import huggingface_hub
import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

from ai_phone import models
from ai_phone.models import LLM, STT, TTS, ModelsError


def plenty_of_disk(monkeypatch, free=100 * 10**9):
    monkeypatch.setattr(models.shutil, "disk_usage", lambda p: types.SimpleNamespace(total=free, used=0, free=free))


def test_needed_depends_on_the_brain():
    assert models.needed("t4", "local") == [STT, TTS, LLM["t4"]]
    assert models.needed("t9", "openai_compatible") == [STT, TTS]


def test_recommended_tier(monkeypatch):
    assert models.recommended_tier(24) == "t4"
    assert models.recommended_tier(32) == "t9"
    assert models.recommended_tier(0) == "t4"
    assert models.resolve_tier("t9") == "t9"
    monkeypatch.setattr(models, "memory_gb", lambda: 64)
    assert models.resolve_tier("auto") == "t9"


def test_memory_gb_reads_sysctl_and_falls_back_to_zero(monkeypatch):
    monkeypatch.setattr(models.subprocess, "check_output", lambda *a, **k: b"25769803776\n")
    assert models.memory_gb() == 24

    def fail(*a, **k):
        raise subprocess.TimeoutExpired("sysctl", 5)
    monkeypatch.setattr(models.subprocess, "check_output", fail)
    assert models.memory_gb() == 0


def test_local_path_needs_config_and_weights(tmp_path, monkeypatch):
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, **k: str(tmp_path))
    assert models.local_path(TTS) is None
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "model.safetensors").write_bytes(b"x")
    assert models.local_path(TTS) == tmp_path

    def not_cached(repo, **k):
        raise LocalEntryNotFoundError("x")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", not_cached)
    assert models.local_path(TTS) is None


def test_missing_lists_absent_repos(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "local_path", lambda r: tmp_path if r == STT else None)
    assert models.missing("t4", "local") == [TTS, LLM["t4"]]


def test_download_size_is_what_the_cache_does_not_hold(monkeypatch):
    class Api:
        def model_info(self, repo, files_metadata):
            assert files_metadata
            return types.SimpleNamespace(siblings=[types.SimpleNamespace(size=300), types.SimpleNamespace(size=None)])
    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(models, "_bytes_on_disk", lambda r: 100 if r == STT else 0)
    assert models.download_size([STT, TTS]) == 500

    class Offline:
        def model_info(self, repo, files_metadata):
            raise OSError("no network")
    monkeypatch.setattr(huggingface_hub, "HfApi", Offline)
    assert models.download_size([STT]) is None


def test_bytes_on_disk_counts_the_repo_blobs(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_hub_root", lambda: tmp_path)
    assert models._bytes_on_disk(TTS) == 0
    blobs = tmp_path / "models--mlx-community--Kokoro-82M-bf16" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "a").write_bytes(b"x" * 7)
    (blobs / "b.incomplete").write_bytes(b"x" * 3)
    assert models._bytes_on_disk(TTS) == 10


def test_pull_downloads_each_repo_and_reports(monkeypatch, tmp_path):
    on_disk = {STT: 0, TTS: 0}
    calls, seen = [], []

    def fake(repo):
        calls.append(repo)
        on_disk[repo] += 50

    monkeypatch.setattr(models, "download_size", lambda repos: 100)
    monkeypatch.setattr(models, "_bytes_on_disk", lambda r: on_disk[r])
    monkeypatch.setattr(models, "_hub_root", lambda: tmp_path)
    plenty_of_disk(monkeypatch)
    models.pull([STT, TTS], report=lambda done, total: seen.append((done, total)), download=fake, poll_sec=0.01)
    assert calls == [STT, TTS]
    assert seen[-1] == (100, 100)
    assert all(0 <= done <= total == 100 for done, total in seen)


def test_pull_refuses_when_the_disk_is_too_small(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(models, "download_size", lambda repos: 5 * 10**9)
    monkeypatch.setattr(models, "_hub_root", lambda: tmp_path / "hub")  # not created yet: the check looks at its parent
    plenty_of_disk(monkeypatch, free=10**9)
    with pytest.raises(ModelsError, match="not enough disk space"):
        models.pull([STT], download=calls.append)
    assert calls == []


def test_pull_wraps_download_errors(monkeypatch, tmp_path):
    def fake(repo):
        raise OSError("boom")

    monkeypatch.setattr(models, "download_size", lambda repos: None)
    monkeypatch.setattr(models, "_hub_root", lambda: tmp_path)
    plenty_of_disk(monkeypatch)
    with pytest.raises(ModelsError, match=f"could not download {TTS}: OSError: boom"):
        models.pull([TTS], download=fake)


def test_pull_uses_the_hub_by_default(monkeypatch, tmp_path):
    got = []
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, **k: got.append(repo))
    monkeypatch.setattr(models, "download_size", lambda repos: 0)
    monkeypatch.setattr(models, "_hub_root", lambda: tmp_path)
    plenty_of_disk(monkeypatch)
    models.pull([STT, TTS], poll_sec=0.01)
    assert got == [STT, TTS]


def test_constants_match_the_backends():
    from ai_phone.services.backends import mlx_kokoro, mlx_lm_backend, mlx_parakeet

    assert LLM["t4"] == mlx_lm_backend.T4
    assert LLM["t9"] == mlx_lm_backend.T9
    assert STT == inspect.signature(mlx_parakeet.ParakeetSTT).parameters["model_id"].default
    assert TTS == inspect.signature(mlx_kokoro.KokoroTTS).parameters["model_id"].default
