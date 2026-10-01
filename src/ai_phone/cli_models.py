"""`ai-phone models pull [--tier <model>]`: download the models for this config (setup time only; networked)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ai_phone import models, paths
from ai_phone.config import BRAIN_KINDS, Config, ConfigError, load_config
from ai_phone.lines.config_writer import read_raw

STOPPED = "Stopped. Run it again to continue where it left off."


def _gb(n: int | None) -> str:
    return "an unknown amount" if n is None else f"{n / 1e9:.1f} GB"


def _why(path: Path, e: Exception) -> str:
    """One line saying what is wrong with the config, for the warning that goes before the download. A file that
    cannot be opened at all (a directory in its place, a half-written binary) says so without a traceback."""
    if isinstance(e, ConfigError):
        return str(e).splitlines()[0]
    return f"config: {path} cannot be read: {getattr(e, 'strerror', None) or type(e).__name__}"


def _wanted() -> tuple[str, str]:
    """models.tier and brain.kind from the config. One that does not load (a voice named before the speech model is
    complete, say) must not stop the download that fixes it: its own values are used when they are valid."""
    path = paths.config_path()
    try:
        cfg = load_config(path) if path.exists() else Config()
        return cfg.models.tier, cfg.brain.kind
    except (ConfigError, OSError, UnicodeDecodeError) as e:
        why = _why(path, e)
    try:
        raw = read_raw(path)
    except Exception:  # not YAML at all
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    tier = (raw.get("models") if isinstance(raw.get("models"), dict) else {}).get("tier")
    kind = (raw.get("brain") if isinstance(raw.get("brain"), dict) else {}).get("kind")
    tier = tier if isinstance(tier, str) and models.known_tier(tier) else None
    kind = kind if kind in BRAIN_KINDS else None
    used = " and ".join(f"{k} {v}" for k, v in (("models.tier", tier), ("brain.kind", kind)) if v)
    print(f"warning: the config does not load ({why}); " + (f"using {used} from it" if used else "using the defaults"))
    return tier or Config().models.tier, kind or Config().brain.kind


def cmd_pull(a) -> int:
    tier, kind = _wanted()
    try:
        tier = models.resolve_tier(a.tier or tier)
    except models.ModelsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    repos = models.missing(tier, kind)
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
    except KeyboardInterrupt:
        # huggingface_hub joins its download threads at exit, so a normal exit would wait for the whole download
        print(f"\n{STOPPED}", flush=True)
        sys.stderr.flush()
        os._exit(130)
    print("\nDone.")
    return 0


def register(sub) -> None:
    m = sub.add_parser("models", help="download the speech and language models (setup time only)")
    ms = m.add_subparsers(dest="models_cmd", required=True)
    p = ms.add_parser("pull", help="download what this config needs and is not on this Mac yet")
    p.add_argument("--tier", help="a key from the model catalog, or a Hub repo id (default: the config's, or by memory)")
    p.set_defaults(func=cmd_pull)
