"""The English Kokoro voices that came with the speech model, and how the TTS backend uses one."""
from __future__ import annotations

import re
from pathlib import Path

DEFAULT = "af_heart"
VOICE_RE = re.compile(r"^[ab][fm]_[a-z]+$")  # a/b: American/British English; f/m: female/male
ACCENTS = {"a": "American", "b": "British"}
GENDERS = {"f": "female", "m": "male"}


def voices_dir() -> Path | None:
    from ai_phone import models
    root = models.local_path(models.TTS)
    return root / "voices" if root is not None and (root / "voices").is_dir() else None


def installed_voices(root: Path | None = None) -> list[str]:
    root = voices_dir() if root is None else root
    if root is None:
        return []
    return sorted(p.stem for p in root.glob("*.safetensors") if VOICE_RE.fullmatch(p.stem))


def voice_path(name: str, root: Path | None = None) -> Path | None:
    root = voices_dir() if root is None else root
    path = root / f"{name}.safetensors" if root is not None else None
    return path if path is not None and path.is_file() else None


def lang_code(name: str) -> str:
    return name[0]  # Kokoro's own codes: "a" American English, "b" British English


def describe(name: str) -> str:
    return f"{ACCENTS[name[0]]} · {GENDERS[name[1]]}"


def title(name: str) -> str:
    return name.split("_", 1)[1].capitalize()
