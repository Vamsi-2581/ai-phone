"""Select a speech backend without importing MLX in the daemon or setup UI."""

TTS_FACTORIES = {
    "kokoro": "ai_phone.services.backends.mlx_kokoro:KokoroTTS",
    "phonon": "ai_phone.services.backends.mlx_phonon:PhononTTS",
}


def tts_factory(engine: str) -> str:
    try:
        return TTS_FACTORIES[engine]
    except KeyError:
        raise ValueError(f"unknown speech engine {engine!r}; use kokoro or phonon") from None
