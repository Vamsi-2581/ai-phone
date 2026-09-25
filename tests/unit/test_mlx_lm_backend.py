"""The parts of the mlx-lm brain that need no weights: how a chat template without a system role is handled."""
from __future__ import annotations

import logging

import pytest

from ai_phone.services.backends.mlx_lm_backend import MlxLmLLM, fold_system

SYS = "You are a phone assistant."


class Tokenizer:
    """apply_chat_template like transformers': `no_system` refuses a standalone system message, as the Gemma and
    Mistral instruct templates do."""

    def __init__(self, no_system: bool = False, broken: bool = False):
        self.no_system, self.broken = no_system, broken
        self.seen: list[list[dict]] = []

    def apply_chat_template(self, messages, add_generation_prompt=True, enable_thinking=False):
        if self.broken:
            raise ValueError("this template is broken")
        if self.no_system and any(m["role"] == "system" for m in messages):
            raise ValueError("System role not supported")
        self.seen.append(list(messages))
        return [len(m["content"]) for m in messages]


def llm(tok: Tokenizer) -> MlxLmLLM:
    out = MlxLmLLM("org/test")
    out._tok = tok
    return out


def test_fold_system_puts_the_instructions_at_the_top_of_the_first_user_turn():
    folded = fold_system([{"role": "system", "content": SYS}, {"role": "user", "content": "Hello?"},
                          {"role": "assistant", "content": "Hi."}, {"role": "user", "content": "Bye"}])
    assert folded == [{"role": "user", "content": f"{SYS}\n\nHello?"},
                      {"role": "assistant", "content": "Hi."}, {"role": "user", "content": "Bye"}]
    assert fold_system([{"role": "user", "content": "Hi"}]) == [{"role": "user", "content": "Hi"}]
    assert fold_system([{"role": "system", "content": SYS}]) == [{"role": "user", "content": SYS}]


def test_a_template_that_refuses_a_system_role_is_detected_and_folded(caplog):
    tok = Tokenizer(no_system=True)
    model = llm(tok)
    assert model._system_role_supported() is False
    with caplog.at_level(logging.INFO):
        model._note_system_role()
    assert "no system role" in caplog.text and "org/test" in caplog.text

    ids = model._ids([{"role": "system", "content": SYS}, {"role": "user", "content": "Hello?"}])
    assert ids and tok.seen[-1] == [{"role": "user", "content": f"{SYS}\n\nHello?"}]


def test_a_template_that_takes_a_system_role_is_left_alone(caplog):
    tok = Tokenizer()
    model = llm(tok)
    assert model._system_role_supported() is True
    with caplog.at_level(logging.INFO):
        model._note_system_role()
    assert caplog.text == ""
    messages = [{"role": "system", "content": SYS}, {"role": "user", "content": "Hello?"}]
    model._ids(messages)
    assert tok.seen[-1] == messages  # untouched


def test_a_template_broken_for_another_reason_is_not_mistaken_for_a_missing_system_role():
    """Folding would not fix it, and pretending it did would hide the real error."""
    model = llm(Tokenizer(broken=True))
    assert model._system_role_supported() is True
    with pytest.raises(ValueError, match="this template is broken"):
        model._ids([{"role": "user", "content": "Hello?"}])


def test_warmup_reports_whether_the_system_role_was_folded(monkeypatch):
    """The fold's log line runs in the worker child, where logging is not configured, so nobody reads it. The
    daemon's ready line prints warmup()'s dict, so the flag has to travel in there."""
    mx = pytest.importorskip("mlx.core", reason="MLX-only (macOS)")
    mlx_lm = pytest.importorskip("mlx_lm", reason="MLX-only (macOS)")
    pytest.importorskip("mlx_lm.models.cache", reason="MLX-only (macOS)")

    from ai_phone.services.backends import mlx_memory

    tok = Tokenizer(no_system=True)
    monkeypatch.setattr(mlx_lm, "load", lambda model_id: (lambda *a, **k: None, tok))
    monkeypatch.setattr(mlx_lm, "stream_generate", lambda *a, **k: iter(()))
    monkeypatch.setattr(mlx_lm.models.cache, "make_prompt_cache", lambda model: [])
    monkeypatch.setattr(mx, "eval", lambda *a, **k: None)
    monkeypatch.setattr(mlx_memory, "begin_wiring", lambda on: None)
    monkeypatch.setattr(mlx_memory, "wire_resident", lambda model, on: {})

    folded = MlxLmLLM("org/no-system").warmup(SYS, wire_memory=False)
    assert folded["system_role_folded"] is True
    assert folded["warm_ms"] > 0

    monkeypatch.setattr(mlx_lm, "load", lambda model_id: (lambda *a, **k: None, Tokenizer()))
    kept = MlxLmLLM("org/with-system").warmup(SYS, wire_memory=False)
    assert kept["system_role_folded"] is False
