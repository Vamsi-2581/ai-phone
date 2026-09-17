"""The models ai-phone runs, whether they are on this Mac, and the setup-time download (`ai-phone models pull`).
Weights live in the Hugging Face cache, where the backends load them from. The daemon never downloads."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

STT = "mlx-community/parakeet-tdt-0.6b-v2"
TTS = "mlx-community/Kokoro-82M-bf16"
LLM = {"t4": "mlx-community/Qwen3.5-4B-MLX-4bit", "t9": "mlx-community/Qwen3.5-9B-MLX-4bit"}
BIG_MEMORY_GB = 32
DOWNLOADING = ".ai-phone-downloading"  # in a repo's cache folder while `pull` has not finished it


class ModelsError(RuntimeError):
    pass


def memory_gb() -> int:
    try:
        return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=5)) // 2**30
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def recommended_tier(mem_gb: int) -> str:
    return "t9" if mem_gb >= BIG_MEMORY_GB else "t4"


def resolve_tier(tier: str) -> str:
    return recommended_tier(memory_gb()) if tier == "auto" else tier


def needed(tier: str, brain_kind: str) -> list[str]:
    return [STT, TTS] + ([LLM[tier]] if brain_kind == "local" else [])


def local_path(repo: str) -> Path | None:
    """The cached snapshot, if it is complete: no `pull` of it was left unfinished, it holds a config and weights,
    and every shard its index names is there. (The cache links each file in as it arrives, so a download stopped
    part-way leaves a snapshot folder behind.)"""
    from huggingface_hub import snapshot_download
    if (_repo_dir(repo) / DOWNLOADING).exists():
        return None
    try:
        path = Path(snapshot_download(repo, local_files_only=True))
    except Exception:  # not cached (LocalEntryNotFoundError) or an unreadable cache: either way, not usable
        return None
    if not (path / "config.json").is_file() or not any(path.glob("*.safetensors")):
        return None
    index = path / "model.safetensors.index.json"
    if index.exists():
        try:
            shards = set(json.loads(index.read_text())["weight_map"].values())
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        if not all((path / shard).is_file() for shard in shards):
            return None
    return path


def missing(tier: str, brain_kind: str) -> list[str]:
    return [r for r in needed(tier, brain_kind) if local_path(r) is None]


def _hub_root() -> Path:
    from huggingface_hub import constants
    return Path(constants.HF_HUB_CACHE)


def _repo_dir(repo: str) -> Path:
    return _hub_root() / ("models--" + repo.replace("/", "--"))


def _bytes_on_disk(repo: str) -> int:
    blobs = _repo_dir(repo) / "blobs"
    total = 0
    try:
        with os.scandir(blobs) as entries:
            for e in entries:
                try:
                    total += e.stat().st_size if e.is_file() else 0
                except OSError:  # a part-file renamed (finished) between the listing and the stat
                    pass
    except OSError:  # no blobs folder yet
        pass
    return total


def download_size(repos: list[str]) -> int | None:
    """Bytes still to fetch: the repos' total size less what the cache already holds. None when the Hub cannot be asked."""
    from huggingface_hub import HfApi
    try:
        total = sum(sum(s.size or 0 for s in HfApi().model_info(r, files_metadata=True).siblings or []) for r in repos)
    except Exception:
        return None
    return max(total - sum(_bytes_on_disk(r) for r in repos), 0)


def pull(repos: list[str], report: Callable[[int, int], None] | None = None, download=None, poll_sec: float = 0.5) -> None:
    """Download each repo into the Hugging Face cache, calling report(bytes done, bytes total) while it runs.
    report is called from a background thread (a UI must hand the update to its own thread); total is 0 when unknown."""
    from huggingface_hub.utils import are_progress_bars_disabled, disable_progress_bars, enable_progress_bars
    if download is None:
        from huggingface_hub import snapshot_download as download
    total = download_size(repos)
    root = _hub_root()
    probe = next((p for p in (root, *root.parents) if p.exists()), Path.home())
    free = shutil.disk_usage(probe).free
    if total is not None and total + 2**30 > free:
        raise ModelsError(f"not enough disk space: {total / 1e9:.1f} GB to download and {free / 1e9:.1f} GB free "
                          "(keep 1 GB spare)")
    start = sum(_bytes_on_disk(r) for r in repos)
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(poll_sec):
            try:
                done = max(sum(_bytes_on_disk(r) for r in repos) - start, 0)
                report(done if total is None else min(done, total), total or 0)  # old blobs can push done past total
            except Exception:  # progress is best effort: one bad tick must not end the watcher
                pass

    bars_were_off = are_progress_bars_disabled()
    disable_progress_bars()  # Hugging Face's own bars would draw over our progress line and the wizard
    watcher = threading.Thread(target=watch, daemon=True, name="models-progress")
    if report:
        watcher.start()
    try:
        for repo in repos:
            marker = _repo_dir(repo) / DOWNLOADING  # removed only once this repo is complete: a stop leaves it
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
                download(repo)
            except Exception as e:
                raise ModelsError(f"could not download {repo}: {type(e).__name__}: {str(e)[:160]}") from None
            marker.unlink(missing_ok=True)
    finally:
        stop.set()
        if report:
            watcher.join()
        if not bars_were_off:
            enable_progress_bars()
    if report:
        report(total or 0, total or 0)
