from pathlib import Path

from ai_phone import voices


def make(root: Path, *names):
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / n).write_bytes(b"x")
    return root


def test_installed_voices_are_the_english_safetensors(tmp_path):
    root = make(tmp_path / "voices", "af_heart.safetensors", "bm_george.safetensors", "af_heart.pt", "jf_alpha.safetensors", "zm_yunxi.safetensors")
    assert voices.installed_voices(root) == ["af_heart", "bm_george"]


def test_no_voice_directory_means_no_voices(monkeypatch):
    monkeypatch.setattr(voices, "voices_dir", lambda: None)
    assert voices.installed_voices() == []
    assert voices.voice_path("af_heart") is None


def test_voice_path_and_names(tmp_path):
    root = make(tmp_path / "voices", "bf_emma.safetensors")
    assert voices.voice_path("bf_emma", root) == root / "bf_emma.safetensors"
    assert voices.voice_path("bf_lily", root) is None
    assert voices.lang_code("bf_emma") == "b" and voices.lang_code("am_adam") == "a"
    assert voices.describe("bf_emma") == "British · female" and voices.describe("am_adam") == "American · male"
    assert voices.title("bf_emma") == "Emma"


def test_voice_re():
    assert voices.VOICE_RE.fullmatch("af_heart") and not voices.VOICE_RE.fullmatch("jf_alpha") and not voices.VOICE_RE.fullmatch("af_")


def test_voices_dir_is_inside_the_speech_model_snapshot(tmp_path, monkeypatch):
    from ai_phone import models
    monkeypatch.setattr(models, "local_path", lambda repo: tmp_path if repo == models.TTS else None)
    assert voices.voices_dir() is None
    make(tmp_path / "voices", "bm_lewis.safetensors")
    assert voices.voices_dir() == tmp_path / "voices"
    assert voices.installed_voices() == ["bm_lewis"]
    monkeypatch.setattr(models, "local_path", lambda repo: None)
    assert voices.voices_dir() is None
