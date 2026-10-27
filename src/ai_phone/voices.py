"""The bundled English voices and their local paths; looking up a voice never downloads it."""
from __future__ import annotations

import re
from pathlib import Path

DEFAULT = "af_heart"
PHONON_DEFAULT = "default"
VOICE_RE = re.compile(r"^[ab][fm]_[a-z]+$")  # a/b: American/British English; f/m: female/male
ACCENTS = {"a": "American", "b": "British"}
GENDERS = {"f": "female", "m": "male"}


def voices_dir() -> Path | None:
    from ai_phone import models
    root = models.local_path(models.TTS)
    return root / "voices" if root is not None and (root / "voices").is_dir() else None


def default_voice(engine: str = "kokoro") -> str:
    return PHONON_DEFAULT if engine == "phonon" else DEFAULT


def installed_voices(root: Path | None = None, *, engine: str = "kokoro") -> list[str]:
    if engine == "phonon":
        return [PHONON_DEFAULT] if voice_path(PHONON_DEFAULT, root, engine=engine) is not None else []
    root = voices_dir() if root is None else root
    if root is None:
        return []
    return sorted(p.stem for p in root.glob("*.safetensors") if VOICE_RE.fullmatch(p.stem))


def voice_path(name: str, root: Path | None = None, *, engine: str = "kokoro") -> Path | None:
    if engine == "phonon":
        from ai_phone import models
        if name != PHONON_DEFAULT:
            return None
        root = models.local_path(models.PHONON) if root is None else root
        path = root / "default-voice.safetensors" if root is not None else None
        return path if path is not None and path.is_file() else None
    root = voices_dir() if root is None else root
    path = root / f"{name}.safetensors" if root is not None else None
    return path if path is not None and path.is_file() else None


def lang_code(name: str) -> str:
    return name[0]  # Kokoro's own codes: "a" American English, "b" British English


def describe(name: str, *, engine: str = "kokoro") -> str:
    if engine == "phonon":
        return "English · bundled voice"
    return f"{ACCENTS[name[0]]} · {GENDERS[name[1]]}"


def title(name: str, *, engine: str = "kokoro") -> str:
    if engine == "phonon":
        return "Default"
    return name.split("_", 1)[1].capitalize()
