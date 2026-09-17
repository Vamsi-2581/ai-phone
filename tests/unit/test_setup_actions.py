"""What the setup flow's actions write and check, against a temporary home: the fake carrier, a fake player, a fake
daemon process and a mock HTTP transport. Nothing here touches the network, plays sound or downloads."""
from __future__ import annotations

import json
import signal
import stat
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest
import yaml

from ai_phone import models, voices
from ai_phone.config import InboundConfig, load_config
from ai_phone.lines.journal import Journal
from ai_phone.providers.base import Number, ProviderError
from ai_phone.providers.fake import OWNED, FakeProvider
from ai_phone.secrets import load_secrets, write_secret
from ai_phone.setup import actions as act
from ai_phone.setup.actions import BRAIN_KEY_ENV, ActionError, Actions, Current, KokoroPlayer, LineResult, Machine
from ai_phone.telephony.state import LineStatus, StateFile

FAKE_KEY = {"FAKE_API_KEY": "fake-key-SECRET"}
BOUGHT = "+442079460000"  # the fake carrier's first search result


class FakePlayer:
    def __init__(self, error: Exception | None = None):
        self.played: list[tuple] = []
        self.error = error

    def play(self, voice, speed, text):
        self.played.append((voice, speed, text))
        if self.error:
            raise self.error


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    monkeypatch.delenv("AI_PHONE_FAKE_PENDING", raising=False)
    monkeypatch.setattr(models, "memory_gb", lambda: 24)
    monkeypatch.setattr(models, "local_path", lambda repo: None)  # nothing downloaded, and never the real cache
    monkeypatch.setattr(voices, "installed_voices", lambda root=None: ["af_heart", "bf_emma", "bm_george"])
    monkeypatch.setattr(act, "probe", lambda path: False)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "_pjsua2.cpython-313-darwin.so").write_bytes(b"")
    monkeypatch.setattr(act, "VENDOR", vendor)
    return tmp_path


def make(home, config: str | None = None, **kw) -> Actions:
    if config is not None:
        (home / "config.yaml").write_text(config)
    kw.setdefault("player", FakePlayer())
    kw.setdefault("provider_loader", lambda name, key, secret=None: FakeProvider(key=key, secret=secret))
    return Actions(home / "config.yaml", **kw)


def raw(home) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text())


# ---- machine and current ---------------------------------------------------------------------

def test_machine_and_current_for_a_new_config(home):
    a = make(home)
    assert a.machine() == Machine(sip_library=True, memory_gb=24, config_exists=False,
                                  models_missing=[models.STT, models.TTS, models.LLM["t4"]])
    inbound = InboundConfig()
    assert a.current() == Current(exists=False, lines={}, brain_kind="local", tier="t4", base_url="http://127.0.0.1:1234/v1",
                                  model="local", brain_key_saved=False, voice="af_heart", speed=1.0,
                                  greeting=inbound.greeting, instructions=inbound.instructions, daily_limit=20)
    for so in act.VENDOR.iterdir():
        so.unlink()
    assert a.machine().sip_library is False


def test_current_reads_the_config(home):
    write_secret("BRAIN_TOKEN_KEY", "sk-saved")
    a = make(home, "models: {tier: t9}\n"
                   "brain: {kind: openai_compatible, base_url: 'https://api.example.com/v1', model: qwen, api_key_env: BRAIN_TOKEN_KEY}\n"
                   "tts: {voice: bm_george, speed: 0.9}\n"
                   "inbound: {greeting: Hello there., instructions: Take a message.}\n"
                   "calls: {daily_limit: 5}\n"
                   "lines:\n  agent: {kind: sip, user: agent, aor: 'sip:agent@192.0.2.10', accept_invites_from: lan}\n")
    cur = a.current()
    assert cur == Current(exists=True, lines={"agent": {"kind": "sip", "user": "agent", "aor": "sip:agent@192.0.2.10",
                                                        "accept_invites_from": "lan"}},
                          brain_kind="openai_compatible", tier="t9", base_url="https://api.example.com/v1", model="qwen",
                          brain_key_saved=True, voice="bm_george", speed=0.9, greeting="Hello there.",
                          instructions="Take a message.", daily_limit=5)
    assert a.machine().models_missing == [models.STT, models.TTS]  # an endpoint brain needs no local language model
    assert a.machine().config_exists
    assert make(home, "calls: {daily_limit: 0}\n").current().daily_limit == 0


def test_current_survives_a_config_that_does_not_load(home):
    a = make(home, "brain: [not, a, mapping\n")
    cur = a.current()
    assert cur.exists and cur.brain_kind == "local" and cur.lines == {}
    assert "not valid YAML" in a.check_config()
    a = make(home, "lines:\n  agent: {kind: sip, aor: nope}\n")
    assert a.current().lines == {"agent": {"kind": "sip", "aor": "nope"}}
    assert a.check_config() == "config.lines.agent.aor: expected sip:user@host[:port]"
    assert make(home, "inbound: {greeting: Hi}\n").check_config() is None


# ---- brain -----------------------------------------------------------------------------------

def test_save_brain_local_writes_its_section_and_keeps_the_others(home):
    a = make(home, "inbound: {greeting: Hi}\nmodels: {keep_warm_sec: 10}\n"
                   "brain: {kind: openai_compatible, base_url: 'https://api.example.com/v1', model: q, connect_timeout_ms: 900}\n")
    a.save_brain_local("t9")
    assert raw(home) == {"inbound": {"greeting": "Hi"}, "models": {"keep_warm_sec": 10, "tier": "t9"}, "brain": {"kind": "local"}}
    assert stat.S_IMODE((home / "config.yaml").stat().st_mode) == 0o600
    assert load_config(home / "config.yaml").models.tier == "t9"


def test_save_brain_endpoint_saves_the_key_and_rolls_back(home):
    a = make(home, "inbound: {greeting: Hi}\n")
    a.save_brain_endpoint("https://api.example.com/v1", "qwen", "sk-new-SECRET")
    assert raw(home)["brain"] == {"kind": "openai_compatible", "base_url": "https://api.example.com/v1", "model": "qwen",
                                  "api_key_env": BRAIN_KEY_ENV}
    assert load_secrets()[BRAIN_KEY_ENV] == "sk-new-SECRET"
    assert a.current().brain_key_saved
    before = (home / "config.yaml").read_text()
    with pytest.raises(ActionError) as e:
        a.save_brain_endpoint("http://203.0.113.5/v1", "qwen", "sk-other-SECRET")
    assert "https" in str(e.value) and "SECRET" not in str(e.value)
    assert (home / "config.yaml").read_text() == before
    assert load_secrets()[BRAIN_KEY_ENV] == "sk-new-SECRET"  # the key that worked is put back
    a.save_brain_endpoint("http://127.0.0.1:8080/v1", "local-model", None)  # no key typed: keep the saved one
    assert raw(home)["brain"] == {"kind": "openai_compatible", "base_url": "http://127.0.0.1:8080/v1", "model": "local-model",
                                  "api_key_env": BRAIN_KEY_ENV}
    assert raw(home)["inbound"] == {"greeting": "Hi"}


def test_save_brain_endpoint_without_a_key_on_a_new_config(home):
    a = make(home)
    with pytest.raises(ActionError, match="must use https"):
        a.save_brain_endpoint("http://203.0.113.5/v1", "qwen", None)
    assert not (home / "config.yaml").exists()
    assert BRAIN_KEY_ENV not in load_secrets()
    a.save_brain_endpoint("http://localhost:1234/v1", "qwen", None)
    assert raw(home) == {"brain": {"kind": "openai_compatible", "base_url": "http://localhost:1234/v1", "model": "qwen"}}
    assert not a.current().brain_key_saved


def test_a_config_that_is_not_yaml_is_never_overwritten(home):
    a = make(home, "brain: [not, a, mapping\n")
    with pytest.raises(ActionError, match="not valid YAML"):
        a.save_brain_local("t4")
    assert (home / "config.yaml").read_text() == "brain: [not, a, mapping\n"


def endpoint(home, handler, **kw):
    return make(home, http=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_endpoint_test_sends_one_short_request(home):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]})

    a = endpoint(home, handler)
    assert a.test_endpoint("https://api.example.com/v1/", "qwen", "sk-test-SECRET") == "OK"
    req = seen[0]
    assert str(req.url) == "https://api.example.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test-SECRET"
    assert json.loads(req.content) == {"model": "qwen", "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 5}
    assert a.test_endpoint("http://127.0.0.1:1234/v1", "qwen", None) == "OK"
    assert "authorization" not in seen[1].headers  # nothing saved: no key
    write_secret(BRAIN_KEY_ENV, "sk-saved-SECRET")
    (home / "config.yaml").write_text(f"brain: {{kind: openai_compatible, base_url: 'http://127.0.0.1:1234/v1', api_key_env: {BRAIN_KEY_ENV}}}\n")
    a.test_endpoint("http://127.0.0.1:1234/v1", "qwen", None)
    assert seen[2].headers["authorization"] == "Bearer sk-saved-SECRET"


def test_endpoint_test_reports_what_the_endpoint_said_without_the_key(home):
    key = "sk-test-SECRET"
    a = endpoint(home, lambda req: httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {key}. " + "x" * 300}}))
    with pytest.raises(ActionError) as e:
        a.test_endpoint("https://api.example.com/v1", "qwen", key)
    msg = str(e.value)
    assert msg.startswith("the endpoint said 401: Incorrect API key provided: ***.")
    assert "SECRET" not in msg and len(msg) <= len("the endpoint said 401: ") + 160
    a = endpoint(home, lambda req: httpx.Response(404, text="no such route"))
    with pytest.raises(ActionError, match="^the endpoint said 404: no such route$"):
        a.test_endpoint("https://api.example.com/v1", "qwen", key)
    a = endpoint(home, lambda req: httpx.Response(200, text="<html>hello</html>"))
    with pytest.raises(ActionError, match="not like an OpenAI chat API"):
        a.test_endpoint("https://api.example.com/v1", "qwen", key)


def test_endpoint_test_that_cannot_connect(home):
    def refuse(req):
        raise httpx.ConnectError("connection refused", request=req)

    a = endpoint(home, refuse)
    with pytest.raises(ActionError) as e:
        a.test_endpoint("http://127.0.0.1:1234/v1", "qwen", "sk-test-SECRET")
    assert str(e.value) == "could not reach 127.0.0.1: ConnectError"


# ---- carriers --------------------------------------------------------------------------------

def test_saved_keys_only_when_all_are_there(home):
    a = make(home)
    assert a.saved_keys("twilio") is None
    write_secret("TWILIO_ACCOUNT_SID", "ACsid")
    assert a.saved_keys("twilio") is None
    write_secret("TWILIO_AUTH_TOKEN", "tok")
    assert a.saved_keys("twilio") == {"TWILIO_ACCOUNT_SID": "ACsid", "TWILIO_AUTH_TOKEN": "tok"}
    assert a.saved_keys("telnyx") is None


def test_owned_and_search_use_the_typed_credentials(home):
    made = []

    def loader(name, key, secret=None):
        made.append((name, key, secret))
        return FakeProvider(key=key, secret=secret)

    a = make(home, provider_loader=loader)
    assert [n.e164 for n in a.owned_numbers("fake", FAKE_KEY)] == [OWNED]
    found = a.search_numbers("fake", FAKE_KEY, "GB", None)
    assert len(found) == 9 and found[0].e164 == BOUGHT
    make(home, provider_loader=loader).owned_numbers("twilio", {"TWILIO_ACCOUNT_SID": "ACsid", "TWILIO_AUTH_TOKEN": "tok"})
    assert made == [("fake", "fake-key-SECRET", None)] * 2 + [("twilio", "ACsid", "tok")]
    assert not (home / "secrets.env").exists()  # checking a key saves nothing


def test_a_key_that_does_not_work(home):
    class Refused(FakeProvider):
        def __init__(self, key="", secret=None):
            super().__init__(key=key, secret=secret)
            self.key = key

        def owned_numbers(self):
            raise ProviderError(f"telnyx GET /phone_numbers -> 401: bad key {self.key}", status=401)

        def search_numbers(self, country, area, limit):
            raise ProviderError("telnyx GET /available_phone_numbers: ConnectError", retryable=True)

    a = make(home, provider_loader=lambda name, key, secret=None: Refused(key=key))
    a_key = {"TELNYX_API_KEY": "KEY-SECRET"}
    with pytest.raises(ActionError) as e:
        a.owned_numbers("telnyx", a_key)
    assert str(e.value) == "That key did not work: telnyx GET /phone_numbers -> 401: bad key ***"
    with pytest.raises(ActionError, match="^Could not reach Telnyx: telnyx GET /available_phone_numbers: ConnectError$"):
        a.search_numbers("telnyx", a_key, "US", "212")

    def no_token(name, key, secret=None):
        raise ProviderError("twilio needs --secret-env (the Auth Token) as well as --key-env (the Account SID)")

    with pytest.raises(ActionError, match="That key did not work"):
        make(home, provider_loader=no_token).owned_numbers("twilio", {"TWILIO_ACCOUNT_SID": "AC"})


def test_buy_number_writes_a_ready_line_quietly(home, capsys):
    a = make(home, "inbound: {greeting: Hi}\n")
    n = a.search_numbers("fake", FAKE_KEY, "GB", None)[0]
    result = a.buy_number("fake", FAKE_KEY, n)
    assert result.line_id == "fake" and result.code == 0
    assert any(f"Line fake is ready: {BOUGHT}" in m for m in result.messages)
    ln = load_config(home / "config.yaml").lines["fake"]
    assert ln.number == BOUGHT and ln.provider.state == "ready" and ln.password_env == "AI_PHONE_LINE_FAKE_PASSWORD"
    saved = load_secrets()
    assert saved["FAKE_API_KEY"] == "fake-key-SECRET" and saved["AI_PHONE_LINE_FAKE_PASSWORD"]
    assert Journal.open(home / "lines", "fake").state == "ready"
    assert raw(home)["inbound"] == {"greeting": "Hi"}
    assert capsys.readouterr() == ("", "")
    other = a.search_numbers("fake", FAKE_KEY, "GB", None)[1]
    assert (a.buy_number("fake", FAKE_KEY, other).line_id, load_config(home / "config.yaml").lines["fake-2"].number) == \
        ("fake-2", other.e164)


def test_a_pending_or_failed_number_comes_back_with_its_messages(home):
    a = make(home, provider_loader=lambda name, key, secret=None: FakeProvider(key=key, pending_polls=1))
    n = a.search_numbers("fake", FAKE_KEY, "GB", None)[0]
    pending = a.buy_number("fake", FAKE_KEY, n)
    assert pending.code == 2 and any("pending" in m for m in pending.messages)
    assert load_config(home / "config.yaml").lines["fake"].provider.state == "pending"
    a = make(home, provider_loader=lambda name, key, secret=None: FakeProvider(key=key, crash_after="connection"))
    failed = a.buy_number("fake", FAKE_KEY, a.search_numbers("fake", FAKE_KEY, "GB", None)[1])
    assert failed == LineResult("fake-2", 3, failed.messages)
    assert "simulated crash after connection" in failed.messages[0] and "lines resume fake-2" in failed.messages[0]


def test_nothing_is_bought_into_a_config_that_does_not_load(home):
    providers = []

    def loader(name, key, secret=None):
        providers.append(FakeProvider(key=key))
        return providers[-1]

    a = make(home, "lines:\n  agent: {kind: sip, aor: nope}\n", provider_loader=loader)
    with pytest.raises(ActionError, match="config.lines.agent.aor"):
        a.buy_number("fake", FAKE_KEY, Number(BOUGHT, 1.0, "GBP", False, "src-0"))
    assert providers[0].created == {}
    assert not (home / "lines" / "fake.journal.json").exists()


def test_attach_number_connects_the_owned_number(home):
    a = make(home)
    owned = a.owned_numbers("fake", FAKE_KEY)[0]
    result = a.attach_number("fake", FAKE_KEY, owned)
    assert (result.line_id, result.code) == ("fake", 0)
    assert load_config(home / "config.yaml").lines["fake"].number == OWNED
    assert Journal.open(home / "lines", "fake").resources()["number_brought"] is True
    stranger = a.attach_number("fake", FAKE_KEY, Number("+15550100009", None, None, False, "x"))
    assert stranger.code == 3 and "not on this fake account" in stranger.messages[0]


def test_forward_adds_the_mobile_and_returns_the_codes(home):
    a = make(home)
    a.buy_number("fake", FAKE_KEY, Number(BOUGHT, 1.0, "GBP", False, "src-0"))
    codes = a.forward("fake", "+447700900123", "busy")
    assert codes == {"activate": f"**67*{BOUGHT}#", "check": "*#67#", "cancel": "##67#", "cancel_all": "##002#"}
    a.forward("fake", "+447700900123", "all")
    a.forward("fake", "+447700900111", "no-answer")
    assert raw(home)["lines"]["fake"]["diverted_from"] == ["+447700900111", "+447700900123"]
    a.add_sip_line("agent", "192.0.2.10")
    with pytest.raises(ActionError, match="no phone number"):
        a.forward("agent", "+447700900123", "busy")


# ---- lines by hand ---------------------------------------------------------------------------

def test_lan_address(home, monkeypatch):
    a = make(home)
    address = a.lan_address()
    assert address.count(".") == 3

    def unreachable(self, addr):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(act.socket.socket, "connect", unreachable)
    assert a.lan_address() == "127.0.0.1"


def test_add_sip_line_picks_a_free_id(home):
    a = make(home)
    assert a.add_sip_line("agent", "192.0.2.10") == "agent"
    assert a.add_sip_line("agent", "192.0.2.10") == "agent-2"
    cfg = load_config(home / "config.yaml")
    assert cfg.lines["agent"].aor == "sip:agent@192.0.2.10" and cfg.lines["agent-2"].user == "agent"
    before = (home / "config.yaml").read_text()
    with pytest.raises(ActionError):
        a.add_sip_line("desk", "not a host")
    assert (home / "config.yaml").read_text() == before


def test_add_trunk_line(home):
    a = make(home)
    assert a.add_trunk_line("sip:sip.example.com", "alex1234", "pw-SECRET", "+15550100000", "tls") == "trunk"
    ln = load_config(home / "config.yaml").lines["trunk"]
    assert (ln.registrar, ln.dial_domain, ln.user, ln.number, ln.transport) == \
        ("sip:sip.example.com", "sip.example.com", "alex1234", "+15550100000", "tls")
    assert ln.password_env == "AI_PHONE_LINE_TRUNK_PASSWORD"
    assert load_secrets()["AI_PHONE_LINE_TRUNK_PASSWORD"] == "pw-SECRET"
    assert "pw-SECRET" not in (home / "config.yaml").read_text()


def test_a_bad_trunk_leaves_no_secret_behind(home):
    a = make(home)
    with pytest.raises(ActionError) as e:
        a.add_trunk_line("sip:bad host", "alex1234", "pw-SECRET", "+15550100000", "udp")
    assert "registrar" in str(e.value) and "SECRET" not in str(e.value)
    assert "AI_PHONE_LINE_TRUNK_PASSWORD" not in load_secrets()
    assert raw(home) == {"lines": {}}
    with pytest.raises(ActionError, match="user"):
        a.add_trunk_line("sip:sip.example.com", "a", "pw-SECRET", "+15550100000", "udp")
    assert "AI_PHONE_LINE_TRUNK_PASSWORD" not in load_secrets()


# ---- voice -----------------------------------------------------------------------------------

def test_voices_and_the_sample(home):
    player = FakePlayer()
    a = make(home, player=player)
    assert a.voices() == ["af_heart", "bf_emma", "bm_george"]
    a.play_sample("bf_emma", 1.1, "Hello.")
    assert player.played == [("bf_emma", 1.1, "Hello.")]
    a = make(home, player=FakePlayer(subprocess.CalledProcessError(1, ["afplay"])))
    with pytest.raises(ActionError, match="^could not play the sample: "):
        a.play_sample("bf_emma", 1.0, "Hello.")
    a = make(home, player=FakePlayer(ActionError("could not play the sample: the speech model is not downloaded")))
    with pytest.raises(ActionError, match="^could not play the sample: the speech model is not downloaded$"):
        a.play_sample("bf_emma", 1.0, "Hello.")


def test_pull_voice_model(home, monkeypatch):
    pulls = []
    monkeypatch.setattr(models, "pull", lambda repos, report=None: pulls.append((repos, report)))
    report = lambda done, total: None  # noqa: E731
    make(home).pull_voice_model(report)
    assert pulls == [([models.TTS], report)]

    def fail(repos, report=None):
        raise models.ModelsError("not enough disk space: 0.4 GB to download and 0.1 GB free (keep 1 GB spare)")

    monkeypatch.setattr(models, "pull", fail)
    with pytest.raises(ActionError, match="^not enough disk space"):
        make(home).pull_voice_model(report)


def test_save_voice(home):
    a = make(home, "tts: {speed: 1.2}\ninbound: {greeting: Hi}\n")
    a.save_voice("bm_george", 0.9)
    assert raw(home) == {"tts": {"speed": 0.9, "voice": "bm_george"}, "inbound": {"greeting": "Hi"}}
    before = (home / "config.yaml").read_text()
    with pytest.raises(ActionError, match="bm_nobody is not installed"):
        a.save_voice("bm_nobody", 1.0)
    assert (home / "config.yaml").read_text() == before


def test_save_callers_merges_into_the_sections(home):
    a = make(home, "calls: {relay_filler_after_ms: 1000}\ninbound: {greeting: Old}\n")
    a.save_callers("Hello there.", "Take a message.", 7)
    assert raw(home)["calls"] == {"relay_filler_after_ms": 1000, "daily_limit": 7}
    assert raw(home)["inbound"] == {"greeting": "Hello there.", "instructions": "Take a message."}
    assert a.current().daily_limit == 7
    before = (home / "config.yaml").read_text()
    with pytest.raises(ActionError, match="greeting"):
        a.save_callers("x" * 501, "Take a message.", 7)
    assert (home / "config.yaml").read_text() == before


# ---- models ----------------------------------------------------------------------------------

def test_missing_models_download_size_and_pull(home, monkeypatch):
    a = make(home, "brain: {kind: local}\nmodels: {tier: t9}\n")
    missing = [models.STT, models.TTS, models.LLM["t9"]]
    assert a.missing_models() == missing
    sizes, pulls = [], []
    monkeypatch.setattr(models, "download_size", lambda repos: sizes.append(repos) or 5_000_000_000)
    monkeypatch.setattr(models, "pull", lambda repos, report=None: pulls.append((repos, report)))
    assert a.download_size() == 5_000_000_000 and sizes == [missing]
    report = lambda done, total: None  # noqa: E731
    a.pull_models(report)
    assert pulls == [(missing, report)]
    monkeypatch.setattr(models, "pull", lambda repos, report=None: (_ for _ in ()).throw(models.ModelsError("could not download x: OSError")))
    with pytest.raises(ActionError, match="^could not download x: OSError$"):
        a.pull_models(report)


# ---- checking the lines ----------------------------------------------------------------------

def status(lid, kind, state, error=None):
    return LineStatus(lid, kind, "", state, None, None, error, time.time())


def write_state(home, *lines):
    StateFile(home / "state.json").write({ln.id: ln for ln in lines}, None, {"kind": "local", "base_url": None})


def test_check_lines_reads_a_running_daemon(home, monkeypatch):
    monkeypatch.setattr(act, "probe", lambda path: True)
    monkeypatch.setattr(act.subprocess, "Popen", None)  # a running daemon is never started again
    write_state(home, status("main", "trunk", "registered"), status("office", "trunk", "degraded", "403 Forbidden"),
                status("new", "trunk", "registering"), status("agent", "sip", "unregistered"),
                status("spare", "trunk", "skipped", "AI_PHONE_LINE_SPARE_PASSWORD not set"),
                status("old", "trunk", "unregistered", "0 OK"))
    assert make(home).check_lines() == [
        ("main", "registered", ""), ("office", "degraded", "403 Forbidden"), ("new", "registering", ""),
        ("agent", "ready", "listening"), ("spare", "skipped", "AI_PHONE_LINE_SPARE_PASSWORD not set"),
        ("old", "unregistered", "0 OK")]
    (home / "state.json").unlink()
    assert make(home).check_lines() == [("lines", "failed", "ai-phone is running but has not written its status yet")]


def test_check_lines_without_the_sip_library(home, monkeypatch):
    for so in act.VENDOR.iterdir():
        so.unlink()
    monkeypatch.setattr(act.subprocess, "Popen", None)
    assert make(home).check_lines() == [("lines", "skipped", "SIP library not built · run ./scripts/install.sh")]


class FakeDaemon:
    """Stands in for `ai-phone run --fake`: each poll writes the next state.json (None: nothing yet); SIGINT stops it."""

    def __init__(self, home, states=(), exit_with=None, log=b"", ignores_sigint=False):
        self.home, self.states, self.exit_with, self.log_text = home, list(states), exit_with, log
        self.ignores_sigint = ignores_sigint
        self.argv = self.signals = None
        self.returncode, self.killed, self.env = None, False, None

    def __call__(self, argv, stdout=None, stderr=None, **kw):
        self.argv, self.signals, self.kw = argv, [], kw
        stdout.write(self.log_text)
        stdout.flush()
        return self

    def poll(self):
        if self.exit_with is not None:
            self.returncode = self.exit_with
        elif self.states:
            lines = self.states.pop(0)
            if lines is not None:
                write_state(self.home, *lines)
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if not self.ignores_sigint:
            self.returncode = -sig

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode

    def kill(self):
        self.killed, self.returncode = True, -9


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(act, "POLL_SEC", 0.01)
    monkeypatch.setattr(act, "CHECK_SEC", 0.5)
    monkeypatch.setattr(act, "STOP_SEC", 0.05)


def test_check_lines_starts_ai_phone_briefly(home, monkeypatch, quick):
    daemon = FakeDaemon(home, states=[None, [status("main", "trunk", "registering"), status("agent", "sip", "unregistered")],
                                      [status("main", "trunk", "registered"), status("agent", "sip", "unregistered")]])
    monkeypatch.setattr(act.subprocess, "Popen", daemon)
    StateFile(home / "state.json").write({"main": status("main", "trunk", "degraded", "stale")}, None, {})
    doc = json.loads((home / "state.json").read_text())
    doc["written_at"] -= 3600  # a state.json from an earlier run is never read
    (home / "state.json").write_text(json.dumps(doc))
    assert make(home).check_lines() == [("main", "registered", ""), ("agent", "ready", "listening")]
    assert daemon.argv == [sys.executable, "-c", "import sys; from ai_phone.cli import main; sys.exit(main(sys.argv[1:]))",
                           "run", "--fake"]
    assert daemon.signals == [signal.SIGINT] and not daemon.killed


def test_check_lines_reports_a_daemon_that_exits(home, monkeypatch, quick):
    log = b"2026-09-17 INFO starting\n" + b"2026-09-17 ERROR could not bind 0.0.0.0:5060: " + b"x" * 300 + b"\n\n"
    daemon = FakeDaemon(home, exit_with=1, log=log)
    monkeypatch.setattr(act.subprocess, "Popen", daemon)
    rows = make(home).check_lines()
    assert rows == [("lines", "failed", ("2026-09-17 ERROR could not bind 0.0.0.0:5060: " + "x" * 300)[:160])]
    assert not daemon.signals  # it already stopped


def test_check_lines_gives_up_after_the_wait_and_stops_it_hard(home, monkeypatch, quick):
    daemon = FakeDaemon(home, states=[[status("main", "trunk", "registering")]] * 1000, ignores_sigint=True)
    monkeypatch.setattr(act.subprocess, "Popen", daemon)
    assert make(home).check_lines() == [("main", "registering", "")]
    assert daemon.signals == [signal.SIGINT] and daemon.killed


def test_check_lines_when_ai_phone_never_reports(home, monkeypatch, quick):
    daemon = FakeDaemon(home)
    monkeypatch.setattr(act.subprocess, "Popen", daemon)
    assert make(home).check_lines() == [("lines", "registering", "ai-phone did not report its lines within 0.5 seconds")]


# ---- the player ------------------------------------------------------------------------------

class Result:
    def __init__(self, audio):
        self.audio = audio


class FakeKokoro:
    def __init__(self):
        self.calls = []

    def generate(self, **kw):
        self.calls.append(kw)
        yield Result(np.full(2400, 0.5, dtype=np.float32))
        yield Result(np.full(1200, -2.0, dtype=np.float32))


def test_kokoro_player_writes_a_wav_and_plays_it(tmp_path, monkeypatch):
    snapshot = tmp_path / "kokoro"
    (snapshot / "voices").mkdir(parents=True)
    (snapshot / "voices" / "bf_emma.safetensors").write_bytes(b"")
    monkeypatch.setattr(models, "local_path", lambda repo: snapshot if repo == models.TTS else None)
    monkeypatch.setattr(voices, "voices_dir", lambda: snapshot / "voices")
    kokoro, loads, played = FakeKokoro(), [], []

    def run(argv, timeout=None, check=False, capture_output=False):
        path = argv[1]
        with wave.open(path, "rb") as w:
            played.append((argv[0], w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes(), timeout, check))
            frames = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        assert frames[0] == 16383 and frames[-1] == -32767  # scaled, and clipped
        played.append(path)

    player = KokoroPlayer(load=lambda path: loads.append(path) or kokoro, run=run)
    player.play("bf_emma", 1.1, "Hello.")
    player.play("bf_emma", 1.0, "Again.")
    assert loads == [str(snapshot)]  # loaded once
    assert played[0] == ("afplay", 1, 2, 24000, 3600, 30, True)
    assert kokoro.calls[0] == {"text": "Hello.", "voice": str(snapshot / "voices" / "bf_emma.safetensors"), "speed": 1.1,
                               "lang_code": "b"}
    assert len(played) == 4 and not any(Path(p).exists() for p in played[1::2])  # the temporary files are gone


def test_kokoro_player_needs_the_model_and_the_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "local_path", lambda repo: None)
    player = KokoroPlayer(load=lambda path: pytest.fail("nothing to load"), run=lambda *a, **k: pytest.fail("nothing to play"))
    with pytest.raises(ActionError, match="^could not play the sample: the speech model is not downloaded$"):
        player.play("af_heart", 1.0, "Hi.")
    monkeypatch.setattr(models, "local_path", lambda repo: tmp_path)
    monkeypatch.setattr(voices, "voices_dir", lambda: None)
    with pytest.raises(ActionError, match="^could not play the sample: the voice bf_emma is not installed$"):
        player.play("bf_emma", 1.0, "Hi.")
