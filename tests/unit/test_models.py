import contextlib
import inspect
import os
import subprocess
import threading
import time
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


def snapshot(tmp_path, monkeypatch):
    """A cache whose Kokoro snapshot holds a config and one weights file."""
    hub = tmp_path / "hub"
    snap = hub / "models--mlx-community--Kokoro-82M-bf16" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    (snap / "model-00001-of-00002.safetensors").write_bytes(b"x")
    monkeypatch.setattr(models, "_hub_root", lambda: hub)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, **k: str(snap))
    return snap


def test_local_path_needs_every_shard_the_index_names(tmp_path, monkeypatch):
    snap = snapshot(tmp_path, monkeypatch)
    assert models.local_path(TTS) == snap  # no index: one file is the whole model
    index = snap / "model.safetensors.index.json"
    index.write_text('{"metadata": {}, "weight_map": {"a.weight": "model-00001-of-00002.safetensors", '
                     '"b.weight": "model-00002-of-00002.safetensors"}}')
    assert models.local_path(TTS) is None
    (snap / "model-00002-of-00002.safetensors").write_bytes(b"x")
    assert models.local_path(TTS) == snap
    index.write_text("{not json")
    assert models.local_path(TTS) is None


def test_an_interrupted_pull_is_not_downloaded(tmp_path, monkeypatch):
    """Quitting a download leaves the files so far; the next check must still count the model as missing."""
    snap = snapshot(tmp_path, monkeypatch)
    plenty_of_disk(monkeypatch)
    monkeypatch.setattr(models, "download_size", lambda repos: 100)
    marker = tmp_path / "hub" / "models--mlx-community--Kokoro-82M-bf16" / ".ai-phone-downloading"

    def stopped(repo):
        assert marker.exists(), "the marker is written before the download starts"
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        models.pull([TTS], download=stopped)
    assert marker.exists() and models.local_path(TTS) is None and models.missing("t4", "openai_compatible") == [TTS]  # (the fake cache serves STT too)
    with pytest.raises(ModelsError):
        models.pull([TTS], download=lambda repo: (_ for _ in ()).throw(OSError("connection reset")))
    assert models.local_path(TTS) is None
    models.pull([TTS], download=lambda repo: None)
    assert not marker.exists() and models.local_path(TTS) == snap


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


def wait_for(cond, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.001)
    return True


def test_pull_downloads_each_repo_and_reports(monkeypatch, tmp_path):
    on_disk = {STT: 0, TTS: 0}
    calls, seen = [], []

    def fake(repo):
        calls.append(repo)
        on_disk[repo] += 50
        # stay "downloading" until the watcher has reported the bytes so far
        assert wait_for(lambda: (sum(on_disk.values()), 100) in seen)

    monkeypatch.setattr(models, "download_size", lambda repos: 100)
    monkeypatch.setattr(models, "_bytes_on_disk", lambda r: on_disk[r])
    monkeypatch.setattr(models, "_hub_root", lambda: tmp_path)
    plenty_of_disk(monkeypatch)
    models.pull([STT, TTS], report=lambda done, total: seen.append((done, total)), download=fake, poll_sec=0.005)
    assert calls == [STT, TTS]
    assert (50, 100) in seen and seen[-1] == (100, 100)
    assert all(0 <= done <= total == 100 for done, total in seen)


def test_progress_survives_a_failing_report_and_stays_within_the_total(monkeypatch, tmp_path):
    on_disk = {STT: 0, TTS: 0}
    seen, threads = [], set()

    def report(done, total):
        threads.add(threading.current_thread().name)
        seen.append((done, total))
        if len(seen) == 1:
            raise RuntimeError("the screen went away for a moment")

    def fake(repo):
        on_disk[repo] += 80  # the cache also holds old blobs, so the bytes on disk pass the total
        assert wait_for(lambda: len(seen) >= 2 and seen[-1] == (min(sum(on_disk.values()), 100), 100))

    monkeypatch.setattr(models, "download_size", lambda repos: 100)
    monkeypatch.setattr(models, "_bytes_on_disk", lambda r: on_disk[r])
    monkeypatch.setattr(models, "_hub_root", lambda: tmp_path)
    plenty_of_disk(monkeypatch)
    models.pull([STT, TTS], report=report, download=fake, poll_sec=0.005)
    assert (80, 100) in seen and seen[-1] == (100, 100)
    assert all(done <= 100 for done, _ in seen)
    assert "models-progress" in threads  # report runs on the watcher thread


def test_bytes_on_disk_skips_a_blob_renamed_while_counting(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_hub_root", lambda: tmp_path)
    blobs = tmp_path / "models--mlx-community--Kokoro-82M-bf16" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "a").write_bytes(b"x" * 7)

    class Renamed:
        def is_file(self):
            return True

        def stat(self):
            raise FileNotFoundError("b.incomplete")

    real = os.scandir

    @contextlib.contextmanager
    def scandir(path):
        with real(path) as entries:
            yield [Renamed(), *entries]

    monkeypatch.setattr(models.os, "scandir", scandir)
    assert models._bytes_on_disk(TTS) == 7


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


def test_pull_uses_the_hub_by_default_without_its_progress_bars(monkeypatch, tmp_path):
    from huggingface_hub.utils import are_progress_bars_disabled

    before = are_progress_bars_disabled()
    got = []
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, **k: got.append((repo, are_progress_bars_disabled())))
    monkeypatch.setattr(models, "download_size", lambda repos: 0)
    monkeypatch.setattr(models, "_hub_root", lambda: tmp_path)
    plenty_of_disk(monkeypatch)
    models.pull([STT, TTS], poll_sec=0.01)
    assert got == [(STT, True), (TTS, True)]  # its own bars would draw over our progress line and the wizard
    assert are_progress_bars_disabled() == before

    def fail(repo, **k):
        raise OSError("boom")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fail)
    with pytest.raises(ModelsError):
        models.pull([STT])
    assert are_progress_bars_disabled() == before


def test_constants_match_the_backends():
    from ai_phone.services.backends import mlx_kokoro, mlx_lm_backend, mlx_parakeet

    assert LLM["t4"] == mlx_lm_backend.T4
    assert LLM["t9"] == mlx_lm_backend.T9
    assert STT == inspect.signature(mlx_parakeet.ParakeetSTT).parameters["model_id"].default
    assert TTS == inspect.signature(mlx_kokoro.KokoroTTS).parameters["model_id"].default
