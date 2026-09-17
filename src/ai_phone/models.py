"""The models ai-phone runs, whether they are on this Mac, and the setup-time download (`ai-phone models pull`).
Weights live in the Hugging Face cache, where the backends load them from. The daemon never downloads."""
from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

STT = "mlx-community/parakeet-tdt-0.6b-v2"
TTS = "mlx-community/Kokoro-82M-bf16"
LLM = {"t4": "mlx-community/Qwen3.5-4B-MLX-4bit", "t9": "mlx-community/Qwen3.5-9B-MLX-4bit"}
BIG_MEMORY_GB = 32


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
    """The cached snapshot, if it holds a config and weights (a download stopped part-way may not)."""
    from huggingface_hub import snapshot_download
    try:
        path = Path(snapshot_download(repo, local_files_only=True))
    except Exception:  # not cached (LocalEntryNotFoundError) or an unreadable cache: either way, not usable
        return None
    return path if (path / "config.json").is_file() and any(path.glob("*.safetensors")) else None


def missing(tier: str, brain_kind: str) -> list[str]:
    return [r for r in needed(tier, brain_kind) if local_path(r) is None]


def _hub_root() -> Path:
    from huggingface_hub import constants
    return Path(constants.HF_HUB_CACHE)


def _bytes_on_disk(repo: str) -> int:
    blobs = _hub_root() / ("models--" + repo.replace("/", "--")) / "blobs"
    return sum(f.stat().st_size for f in blobs.iterdir() if f.is_file()) if blobs.is_dir() else 0


def download_size(repos: list[str]) -> int | None:
    """Bytes still to fetch: the repos' total size less what the cache already holds. None when the Hub cannot be asked."""
    from huggingface_hub import HfApi
    try:
        total = sum(sum(s.size or 0 for s in HfApi().model_info(r, files_metadata=True).siblings or []) for r in repos)
    except Exception:
        return None
    return max(total - sum(_bytes_on_disk(r) for r in repos), 0)


def pull(repos: list[str], report: Callable[[int, int], None] | None = None, download=None, poll_sec: float = 0.5) -> None:
    """Download each repo into the Hugging Face cache, calling report(bytes done, bytes total) while it runs."""
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
            if report:
                report(max(sum(_bytes_on_disk(r) for r in repos) - start, 0), total or 0)

    watcher = threading.Thread(target=watch, daemon=True, name="models-progress")
    watcher.start()
    try:
        for repo in repos:
            try:
                download(repo)
            except Exception as e:
                raise ModelsError(f"could not download {repo}: {type(e).__name__}: {str(e)[:160]}") from None
    finally:
        stop.set()
        watcher.join()
    if report:
        report(total or 0, total or 0)
