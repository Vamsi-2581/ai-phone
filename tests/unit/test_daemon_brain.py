"""Which backend the daemon starts for each kind of brain, what it tells the person at start-up, and what it writes
to state.json. The workers themselves are not started here (tests/integration does that)."""
from pathlib import Path

import pytest

from ai_phone.config import BrainConfig, Config, ConversationConfig, ModelsConfig
from ai_phone.daemon import Daemon, brain_warning, drain_timeout_ms, llm_worker, subscription_first_token_ms

PROMPT = "You are an AI assistant speaking on a live phone call."
WIRE = {"wire_memory": True}


def spec(kind, *, fake=False, tier="t4", **brain):
    cfg = Config(brain=BrainConfig(kind=kind, **brain), models=ModelsConfig(keep_warm_sec=30))
    return llm_worker(cfg, tier, fake, PROMPT, {"AI_PHONE_BRAIN_API_KEY": "sk-secret"}, dict(WIRE))


def test_a_brain_on_this_mac_loads_the_weights_the_tier_names():
    s = spec("local", tier="t9")
    assert s["factory"] == "ai_phone.services.backends.mlx_lm_backend:make_llm"
    assert s["factory_args"]["model_id"].count("/") == 1 and s["warm_args"] == {"system_prompt": PROMPT, **WIRE}
    assert s["keep_warm_sec"] == 30


def test_a_claude_subscription_runs_claude_code():
    s = spec("claude", model="haiku", cli="/opt/homebrew/bin/claude")
    assert s["factory"] == "ai_phone.services.backends.cli_brain:make_claude"
    assert s["factory_args"] == {"model": "haiku", "cli": "/opt/homebrew/bin/claude", "first_token_timeout_ms": 12000,
                                 "stream_idle_timeout_ms": 4000, "max_response_chars": 2000, "drain_timeout_ms": 4000}
    assert s["warm_args"] == {"system_prompt": PROMPT}  # no wire_memory: there are no weights on this Mac
    assert s["keep_warm_sec"] == 0.0                   # and nothing to keep warm


def test_a_cancelled_turn_may_take_a_third_of_the_callers_patience_to_finish():
    """Claude Code cannot be interrupted, so the next turn waits for the one the caller talked over. What is spent
    waiting comes out of the time this turn has before the controller apologises."""
    assert drain_timeout_ms(Config()) == 4000  # reply_timeout_sec 12
    assert drain_timeout_ms(Config(conversation=ConversationConfig(reply_timeout_sec=6))) == 2000
    assert spec("claude")["factory_args"]["drain_timeout_ms"] == 4000


def test_a_chatgpt_subscription_runs_codex():
    s = spec("chatgpt", model="gpt-5.6-luna")
    assert s["factory"] == "ai_phone.services.backends.cli_brain:make_codex"
    assert s["factory_args"]["model"] == "gpt-5.6-luna" and s["factory_args"]["cli"] is None


def test_a_plan_gets_twelve_seconds_for_its_first_word_unless_the_person_said_otherwise():
    assert subscription_first_token_ms(BrainConfig(kind="claude")) == 12000
    assert subscription_first_token_ms(BrainConfig(kind="claude", first_token_timeout_ms=6000)) == 6000
    assert spec("claude", first_token_timeout_ms=6000)["factory_args"]["first_token_timeout_ms"] == 6000


def test_an_endpoint_brain_is_the_server_the_config_names_even_with_fake_models():
    """--fake stands in for what runs on this Mac; an endpoint is a server the person chose."""
    for fake in (False, True):
        s = spec("openai_compatible", fake=fake, base_url="http://127.0.0.1:1234/v1", model="qwen",
                 api_key_env="AI_PHONE_BRAIN_API_KEY")
        assert s["factory"] == "ai_phone.services.backends.openai_compat:make_openai"
        assert s["factory_args"]["api_key"] == "sk-secret" and s["factory_args"]["base_url"].endswith("/v1")


@pytest.mark.parametrize("kind", ["local", "claude", "chatgpt"])
def test_fake_models_never_spend_a_turn_of_the_plan(kind):
    s = spec(kind, fake=True)
    assert s["factory"] == "ai_phone.services.backends.fake:FakeLLM"
    assert s["factory_args"] is None and s["warm_args"] == {"system_prompt": PROMPT}


def test_the_start_up_warning_says_where_the_words_go():
    assert brain_warning(Config()) is None
    assert brain_warning(Config(brain=BrainConfig(kind="openai_compatible", base_url="http://127.0.0.1:1234/v1"))) is None
    endpoint = brain_warning(Config(brain=BrainConfig(kind="openai_compatible", base_url="https://api.example.com/v1")))
    assert "api.example.com" in endpoint and "not audio" in endpoint
    claude = brain_warning(Config(brain=BrainConfig(kind="claude")))
    assert claude == ("Brain: your Claude subscription. What callers say, the instructions and the conversation are "
                      "sent to Anthropic through Claude Code (not audio).")
    chatgpt = brain_warning(Config(brain=BrainConfig(kind="chatgpt")))
    assert "to OpenAI through Codex (not audio)." in chatgpt and "ChatGPT subscription" in chatgpt


def test_a_subscription_brain_is_not_on_this_mac_so_the_prompt_says_no_host(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    local = Daemon(Config(), "t4", tmp_path, lan=True, fake=True)
    plan = Daemon(Config(brain=BrainConfig(kind="chatgpt")), "t4", tmp_path, lan=True, fake=True)
    assert local.remote_brain is False and plan.remote_brain is True
    assert "You are running on " in local.fixed_prompt()
    assert "You are running on " not in plan.fixed_prompt()


def test_state_json_names_the_brain_the_daemon_is_using(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    d = Daemon(Config(brain=BrainConfig(kind="claude", model="haiku")), "t4", tmp_path, lan=True, fake=False)
    d._write_state()
    assert d.state_file.read(d.state_file.path)["brain"] == {"kind": "claude", "base_url": None, "model": "haiku"}
    d = Daemon(Config(brain=BrainConfig(kind="openai_compatible", base_url="http://127.0.0.1:1234/v1", model="qwen")),
               "t4", tmp_path, lan=True, fake=False)
    d._write_state()
    assert d.state_file.read(d.state_file.path)["brain"] == {"kind": "openai_compatible",
                                                            "base_url": "http://127.0.0.1:1234/v1", "model": "qwen"}
