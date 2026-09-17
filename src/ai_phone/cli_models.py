"""`ai-phone models pull [--tier t4|t9]`: download the models for this config (setup time only; networked)."""
from __future__ import annotations

import sys

from ai_phone import models, paths
from ai_phone.config import Config, load_config


def _gb(n: int | None) -> str:
    return "an unknown amount" if n is None else f"{n / 1e9:.1f} GB"


def cmd_pull(a) -> int:
    cfg = load_config(paths.config_path()) if paths.config_path().exists() else Config()
    tier = a.tier or models.resolve_tier(cfg.models.tier)
    repos = models.missing(tier, cfg.brain.kind)
    if not repos:
        print("All models are already downloaded.")
        return 0
    print(f"Downloading {len(repos)} model(s), {_gb(models.download_size(repos))}, into the Hugging Face cache:")
    for r in repos:
        print(f"  {r}")
    tty = sys.stdout.isatty()

    def report(done: int, total: int) -> None:
        if total and tty:
            print(f"\r  {done * 100 // total:3d}%  {done / 1e9:.1f} of {total / 1e9:.1f} GB", end="", flush=True)

    try:
        models.pull(repos, report)
    except models.ModelsError as e:
        print(f"\nerror: {e}")
        return 3
    print("\nDone.")
    return 0


def register(sub) -> None:
    m = sub.add_parser("models", help="download the speech and language models (setup time only)")
    ms = m.add_subparsers(dest="models_cmd", required=True)
    p = ms.add_parser("pull", help="download what this config needs and is not on this Mac yet")
    p.add_argument("--tier", choices=["t4", "t9"], help="the local model size (default: the config's, or by memory)")
    p.set_defaults(func=cmd_pull)
