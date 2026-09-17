"""The setup conversation driven by scripted answers against fake actions: which questions come, in what order, and
what each answer saves."""
from __future__ import annotations

from pathlib import Path

import pytest

import ai_phone
from ai_phone.config import InboundConfig
from ai_phone.lines.forward import divert_codes
from ai_phone.pipeline.prompts import DISCLOSURE
from ai_phone.providers.base import Number
from ai_phone.setup import flow
from ai_phone.setup.actions import ActionError, Current, LineResult, Machine
from ai_phone.setup.flow import BRAIN, CALLERS, CHECK, DONE, LINE, VOICE, WELCOME
from ai_phone.setup.ui import BaseUI, Cancel, Check, Page

GREETING, INSTRUCTIONS = InboundConfig().greeting, InboundConfig().instructions
VOICES = ["af_bella", "af_heart", "am_adam", "bf_emma", "bm_george"]
OWNED = [Number("+15550100007", None, None, False, "PN7"), Number("+15550100008", None, None, False, "PN8")]
FOUND = [Number("+15550100001", 1.15, "USD", False, "n1"), Number("+15550100002", 1.15, "USD", True, "n2")]
TWILIO = {"TWILIO_ACCOUNT_SID": "ACsid", "TWILIO_AUTH_TOKEN": "tokSECRET"}
SIP_LINE = {"kind": "sip", "user": "agent", "aor": "sip:agent@192.0.2.10"}
NUMBER_LINE = {"kind": "trunk", "number": "+15550100001", "provider": {"name": "twilio", "state": "ready"}}


class Seq:
    """Successive results of one action: each call takes the next one (an exception is raised)."""

    def __init__(self, *items):
        self.items = list(items)


class ScriptedUI(BaseUI):
    """Answers are (question substring, answer) pairs, consumed in order. An answer is an option label (choose),
    a list of strings (ask; "" takes the default), "<default>", "<back>" or "<quit>"."""

    def __init__(self, answers):
        super().__init__()
        self.answers = list(answers)
        self.pages: list[Page] = []
        self.menus: list[tuple[str, list, int, bool]] = []
        self.fields = []
        self.labels: list[str] = []
        self.steps: list[int] = []
        self.closed = False

    def begin(self, step):
        super().begin(step)
        self.steps.append(step)

    def _answer(self, page, back=True):
        self.next_question()
        self.pages.append(page)
        assert self.answers, f"no answer left for {page.question!r}"
        want, answer = self.answers.pop(0)
        assert want in page.question, f"expected a question with {want!r}, got {page.question!r}"
        if answer == "<back>":
            assert back, f"{page.question!r} has no Back"
            raise self.back()
        if answer == "<quit>":
            raise Cancel()
        return answer

    def choose(self, page, options, default=0, back=True):
        self.menus.append((page.question, options, default, back))
        answer = self._answer(page, back)
        if answer == "<default>":
            return default
        labels = [o.label for o in options]
        assert answer in labels, f"{answer!r} is not one of {labels} on {page.question!r}"
        return labels.index(answer)

    def ask(self, page, fields):
        self.fields.extend(fields)
        answer = self._answer(page)
        assert len(answer) == len(fields)
        values = []
        for typed, f in zip(answer, fields):
            value = typed or f.default
            error = f.validate(value) if f.validate else None
            assert error is None, f"{f.label}={value!r}: {error}"
            values.append(value)
        return values

    def busy(self, page, label, work):
        self.pages.append(page)
        self.labels.append(label)
        return work()

    def progress(self, page, label, work):
        self.pages.append(page)
        self.labels.append(label)
        return work(lambda done, total: None)

    def close(self):
        self.closed = True

    def menu(self, question):
        return next(m for m in self.menus if question in m[0])

    def questions(self, text):
        return [p for p in self.pages if text in p.question]


class FakeActions:
    def __init__(self, *, exists=False, memory=24, missing=(), sip_library=True, lines=None, **results):
        self.calls: list[tuple[str, tuple]] = []
        self.m = Machine(sip_library=sip_library, memory_gb=memory, models_missing=list(missing), config_exists=exists)
        self.cur = Current(exists=exists, lines=dict(lines or {}), brain_kind="local", tier="t4",
                           base_url="http://127.0.0.1:1234/v1", model="local", brain_key_saved=False, voice="af_heart",
                           speed=1.0, greeting=GREETING, instructions=INSTRUCTIONS, daily_limit=20)
        self.results = {"saved_keys": None, "owned_numbers": OWNED, "search_numbers": FOUND, "lan_address": "192.0.2.10",
                        "voices": VOICES, "check_config": None, "download_size": 4_300_000_000, "test_endpoint": "OK"}
        self.results.update(results)

    def _call(self, name, *args):
        self.calls.append((name, args))
        r = self.results.get(name)
        if isinstance(r, Seq):
            r = r.items.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    def args(self, name):
        return [a for n, a in self.calls if n == name]

    def changes(self):
        return [n for n, _ in self.calls if n.startswith(("save_", "add_", "buy_", "attach_", "forward"))]

    # what the flow reads
    def machine(self):
        return self.m

    def current(self):
        return self.cur

    def saved_keys(self, carrier):
        return self._call("saved_keys", carrier)

    def lan_address(self):
        return self._call("lan_address")

    def voices(self):
        return self._call("voices")

    def missing_models(self):
        return self._call("missing_models") or self.m.models_missing

    def download_size(self):
        return self._call("download_size")

    def check_config(self):
        return self._call("check_config")

    def check_lines(self):
        r = self._call("check_lines")
        return r if r is not None else [(lid, "ready", "listening") for lid in self.cur.lines]

    # what the flow tries
    def test_endpoint(self, url, model, key):
        return self._call("test_endpoint", url, model, key)

    def owned_numbers(self, carrier, creds):
        return self._call("owned_numbers", carrier, dict(creds))

    def search_numbers(self, carrier, creds, country, area):
        return self._call("search_numbers", carrier, dict(creds), country, area)

    def play_sample(self, voice, speed, text):
        return self._call("play_sample", voice, speed, text)

    def pull_voice_model(self, report):
        self._call("pull_voice_model", report)
        report(1, 1)

    def pull_models(self, report):
        self._call("pull_models", report)
        report(1, 2)

    # what the flow saves
    def save_brain_local(self, tier):
        self._call("save_brain_local", tier)
        self.cur.brain_kind, self.cur.tier = "local", tier

    def save_brain_endpoint(self, url, model, key):
        self._call("save_brain_endpoint", url, model, key)
        self.cur.brain_kind, self.cur.base_url, self.cur.model = "openai_compatible", url, model

    def _line(self, name, carrier, creds, number):
        r = self._call(name, carrier, dict(creds), number) or LineResult(carrier, 0, [f"Line {carrier} is ready: {number.e164}."])
        if r.code in (0, 2):
            self.cur.lines[r.line_id] = {"kind": "trunk", "number": number.e164, "provider": {"name": carrier, "state": "ready"}}
        return r

    def buy_number(self, carrier, creds, number):
        return self._line("buy_number", carrier, creds, number)

    def attach_number(self, carrier, creds, number):
        return self._line("attach_number", carrier, creds, number)

    def forward(self, line_id, mobile, when):
        self._call("forward", line_id, mobile, when)
        return divert_codes(when, self.cur.lines[line_id]["number"])

    def add_sip_line(self, name, host):
        line_id = self._call("add_sip_line", name, host) or name
        self.cur.lines[line_id] = {"kind": "sip", "user": name, "aor": f"sip:{name}@{host}"}
        return line_id

    def add_trunk_line(self, registrar, user, password, number, transport):
        line_id = self._call("add_trunk_line", registrar, user, password, number, transport) or "trunk"
        self.cur.lines[line_id] = {"kind": "trunk", "number": number}
        return line_id

    def save_voice(self, voice, speed):
        self._call("save_voice", voice, speed)
        self.cur.voice, self.cur.speed = voice, speed

    def save_callers(self, greeting, instructions, daily_limit):
        self._call("save_callers", greeting, instructions, daily_limit)


def page_text(page: Page) -> str:
    return repr(page)


@pytest.fixture(autouse=True)
def default_home(monkeypatch):
    """The flow only shows paths (the fake actions write nothing), so the default home is safe and gives `~` paths."""
    monkeypatch.delenv("AI_PHONE_HOME", raising=False)


WELCOME_NEW = ("Give your AI a phone number", "Continue")
FINISH = ("You're set up", "Finish")


def test_new_setup_local_brain_sip_line_default_voice():
    ui = ScriptedUI([
        WELCOME_NEW,
        ("Where should your agent think?", "On this Mac"),
        ("Which model size?", "<default>"),
        ("How should people reach your agent?", "A SIP address"),
        ("Name your SIP address", ["agent", "192.0.2.10"]),
        ("How should your agent sound?", "American · female"),
        ("American · female", "<default>"),
        ("Heart", "Use this voice"),
        ("How fast should it talk?", [""]),
        ("What should callers hear?", ["", "", ""]),
        ("Everything works", "Continue"),
        FINISH,
    ])
    actions = FakeActions()
    outcome = flow.run(ui, actions)
    assert outcome.code == 0 and ui.closed and not ui.answers
    assert ui.steps == [WELCOME, BRAIN, LINE, VOICE, CALLERS, CHECK, DONE]
    _question, options, default, _back = ui.menu("Which model size?")
    assert options[default].label == "Qwen 4B"
    assert ui.questions("Which model size?")[0].detail == "This Mac has 24 GB of memory, so Qwen 4B is recommended."
    assert [o.label for o in ui.menu("How should people reach")[1]][3] == "Skip"
    assert ui.menu("How should people reach")[1][3].hint == "add one later"
    group = ui.menu("American · female")
    assert [o.label for o in group[1]] == ["Bella", "Heart"] and group[2] == 1 and group[1][1].hint == "current"
    assert ui.questions("Heart")[0].detail == "American · female"
    speed = next(f for f in ui.fields if f.label == "Speed")
    assert speed.default == "1.0"
    assert actions.changes() == ["save_brain_local", "add_sip_line", "save_voice", "save_callers"]
    assert actions.args("save_brain_local") == [("t4",)]
    assert actions.args("add_sip_line") == [("agent", "192.0.2.10")]
    assert actions.args("save_voice") == [("af_heart", 1.0)]
    assert actions.args("save_callers") == [(GREETING, INSTRUCTIONS, 20)]
    assert actions.args("play_sample") == [("af_heart", 1.0, DISCLOSURE)]
    check = ui.questions("Everything works")[0]
    assert check.checks == [Check("Models", "downloaded", True), Check("Config", "loads", True),
                            Check("Line agent", "ready · listening", True), Check("Voice", "Heart · played", True)]
    assert "uv run ai-phone run" in outcome.summary
    assert "pjsua --local-port 5070 sip:agent@127.0.0.1" in outcome.summary
    assert ui.questions("You're set up")[0].body == outcome.summary


def test_welcome_shows_the_machine_checks():
    ui = ScriptedUI([("Give your AI", "Cancel")])
    outcome = flow.run(ui, FakeActions(memory=8, sip_library=False, missing=["a/b"]))
    assert outcome.code == 1
    assert outcome.summary == "Setup cancelled. What earlier steps saved is kept; nothing else was changed."
    page = ui.pages[0]
    assert page.detail == "Calls answered and placed by models running on this Mac."
    assert page.note == "Picks a line, a brain and a voice, then checks it all works."
    assert page.checks == [
        Check("SIP library", "not built yet · run ./scripts/install.sh", False),
        Check("Memory", "8 GB · 4B model recommended", None),
        Check("Models", "not downloaded yet · setup can download them", None),
        Check("Config", "new · ~/.ai-phone/", True),
    ]
    assert [(o.label, o.hint) for o in ui.menus[0][1]] == [("Continue", ""), ("Cancel", "exit setup, nothing is saved")]

    ui = ScriptedUI([("Give your AI", "Cancel")])
    flow.run(ui, FakeActions(memory=36, exists=True))
    assert ui.pages[0].checks == [
        Check("SIP library", "built", True),
        Check("Memory", "36 GB · 9B model recommended", True),
        Check("Models", "downloaded", True),
        Check("Config", "found · ~/.ai-phone/ (setup changes only what you pick)", True),
    ]
    assert [o.label for o in ui.menus[0][1]] == ["Add a line", "Change the brain", "Change the voice",
                                                "Change what callers hear", "Run all steps", "Cancel"]


def test_endpoint_brain_retries_after_a_failed_test():
    ui = ScriptedUI([
        WELCOME_NEW,
        ("Where should your agent think?", "My own endpoint"),
        ("Your endpoint", ["http://127.0.0.1:8080/v1", "qwen", "sk-test"]),
        ("Your endpoint", ["", "", "sk-test"]),
        ("How should people reach your agent?", "<quit>"),
    ])
    actions = FakeActions(test_endpoint=Seq(ActionError("could not reach 127.0.0.1: ConnectError"), "OK"))
    assert flow.run(ui, actions).code == 1
    first, _busy, second, _busy2 = ui.questions("Your endpoint")
    assert first.error == "" and second.error == "could not reach 127.0.0.1: ConnectError"
    assert ui.labels == ["Sending one short test request to 127.0.0.1…"] * 2
    assert actions.args("test_endpoint") == [("http://127.0.0.1:8080/v1", "qwen", "sk-test")] * 2
    assert actions.args("save_brain_endpoint") == [("http://127.0.0.1:8080/v1", "qwen", "sk-test")]
    retry = ui.fields[3:]
    assert [f.default for f in retry] == ["http://127.0.0.1:8080/v1", "qwen", ""]
    assert retry[2].secret and retry[2].placeholder == "leave empty for none"
    assert all("sk-test" not in page_text(p) for p in ui.pages)
    assert all("sk-test" not in repr(f) for f in ui.fields)


def test_endpoint_brain_keeps_the_saved_key_when_left_empty():
    ui = ScriptedUI([
        ("Give your AI", "Change the brain"),
        ("Where should your agent think?", "<default>"),
        ("Your endpoint", ["https://api.example.com/v1", "", ""]),
        ("A few things need attention", "<quit>"),
    ])
    actions = FakeActions(exists=True)
    actions.cur.brain_kind, actions.cur.model, actions.cur.brain_key_saved = "openai_compatible", "qwen", True
    assert flow.run(ui, actions).code == 1
    assert ui.menus[1][2] == 1  # the current brain is the default
    assert ui.fields[2].placeholder == "leave empty to keep the saved key"
    assert actions.args("test_endpoint") == [("https://api.example.com/v1", "qwen", None)]
    assert actions.args("save_brain_endpoint") == [("https://api.example.com/v1", "qwen", None)]
    assert ui.labels[0] == "Sending one short test request to api.example.com…"


def test_endpoint_brain_that_does_not_validate_is_asked_again():
    ui = ScriptedUI([
        ("Give your AI", "Change the brain"),
        ("Where should your agent think?", "My own endpoint"),
        ("Your endpoint", ["http://203.0.113.5/v1", "qwen", ""]),
        ("Your endpoint", ["https://203.0.113.5/v1", "", ""]),
        ("A few things need attention", "<quit>"),
    ])
    https = "config.brain.base_url: a brain that is not on this machine must use https"
    actions = FakeActions(exists=True, save_brain_endpoint=Seq(ActionError(https), None))
    flow.run(ui, actions)
    assert ui.questions("Your endpoint")[2].error == https
    assert actions.args("save_brain_endpoint")[-1] == ("https://203.0.113.5/v1", "qwen", None)


def add_a_line(carrier="Twilio"):
    return [("Give your AI", "Add a line"), ("How should people reach your agent?", "A phone number"),
            ("Which carrier?", carrier)]


def test_twilio_new_number_buys_only_after_confirmation():
    choose_a_number = [
        ("Your Twilio credentials", ["ACsid", "tokSECRET"]),
        ("A new number or one you already have?", "A new number"),
        ("Where should the number be?", ["", ""]),
        ("Pick a number", "+15550100001"),
    ]
    ui = ScriptedUI([
        *add_a_line(), *choose_a_number,
        ("Buy +15550100001 for $1.15/month?", "<back>"),
        ("How should people reach your agent?", "A phone number"), ("Which carrier?", "Twilio"), *choose_a_number,
        ("Buy +15550100001 for $1.15/month?", "Buy +15550100001"),
        ("Also forward your own mobile to it?", "No"),
        ("Everything works", "Continue"),
        FINISH,
    ])
    actions = FakeActions(exists=True, lines={}, check_lines=[("twilio", "registered", "")])
    outcome = flow.run(ui, actions)
    assert outcome.code == 0
    assert ui.steps == [WELCOME, LINE, LINE, CHECK, DONE]
    assert actions.args("buy_number") == [("twilio", TWILIO, FOUND[0])]
    assert actions.args("owned_numbers") == [("twilio", TWILIO)] * 2
    assert actions.args("search_numbers") == [("twilio", TWILIO, "US", None)] * 2
    credentials = ui.questions("Your Twilio credentials")[0]
    assert credentials.detail == "Saved to ~/.ai-phone/secrets.env, readable only by you. Never shown or sent anywhere else."
    assert [(f.label, f.secret, f.placeholder) for f in ui.fields[:2]] == [("Account SID", True, "AC…"), ("Auth Token", True, "32 characters")]
    assert ui.questions("A new number or one")[0].checks == [Check("Twilio key", "works · 2 numbers on this account", True)]
    assert [(o.label, o.hint) for o in ui.menu("A new number or one")[1]] == [
        ("A new number", "search and buy one"), ("One I already have", "2 on this account")]
    assert [(o.label, o.hint) for o in ui.menu("Pick a number")[1]] == [
        ("+15550100001", "$1.15/month"), ("+15550100002", "$1.15/month · documents required")]
    confirm = ui.questions("Buy +15550100001")[0]
    assert confirm.detail == "Twilio bills your account every month until you remove the line (ai-phone lines remove)."
    assert [(o.label, o.hint) for o in ui.menu("Buy +15550100001")[1]] == [("Buy +15550100001", "$1.15/month")]
    assert ui.labels[-4:-2] == ["Looking for numbers…", "Buying +15550100001 and connecting it…"]
    assert ui.questions("Setting up +15550100001")
    assert all("tokSECRET" not in page_text(p) and "ACsid" not in page_text(p) for p in ui.pages)
    assert "call +15550100001 from any phone (your normal call rates)" in outcome.summary


def test_no_numbers_found_asks_for_another_area():
    ui = ScriptedUI([
        *add_a_line(),
        ("Your Twilio credentials", ["ACsid", "tokSECRET"]),
        ("A new number or one you already have?", "A new number"),
        ("Where should the number be?", ["gb", "999"]),
        ("Where should the number be?", ["", ""]),
        ("Pick a number", "<quit>"),
    ])
    actions = FakeActions(exists=True, search_numbers=Seq([], FOUND))
    flow.run(ui, actions)
    first, _, second, _ = ui.questions("Where should the number be?")
    assert second.error == "No numbers found there. Try another area code, or leave it empty."
    assert actions.args("search_numbers") == [("twilio", TWILIO, "GB", "999"), ("twilio", TWILIO, "GB", None)]


def test_telnyx_existing_number_and_forward():
    ui = ScriptedUI([
        *add_a_line("Telnyx"),
        ("Use the saved Telnyx key?", "Use the saved key"),
        ("A new number or one you already have?", "One I already have"),
        ("Which number?", "+15550100008"),
        ("Connect +15550100008 to ai-phone?", "Connect +15550100008"),
        ("Also forward your own mobile to it?", "Yes"),
        ("Your mobile number", ["+447700900123"]),
        ("When should calls go to your agent?", "When I'm busy"),
        ("Dial this on your mobile", "Done"),
        ("Everything works", "Continue"),
        FINISH,
    ])
    saved = {"TELNYX_API_KEY": "KEYsaved"}
    actions = FakeActions(exists=True, saved_keys=saved)
    assert flow.run(ui, actions).code == 0
    assert not ui.questions("credentials")
    assert ui.questions("Use the saved Telnyx key?")[0].detail == "It is in ~/.ai-phone/secrets.env."
    assert actions.args("attach_number") == [("telnyx", saved, OWNED[1])]
    assert not actions.args("buy_number")
    assert [(o.label, o.hint) for o in ui.menu("Connect +15550100008")[1]] == [("Connect +15550100008", "free")]
    assert ui.labels[:2] == ["Checking your Telnyx account…", "Connecting +15550100008…"]
    assert actions.args("forward") == [("telnyx", "+447700900123", "busy")]
    codes = ui.questions("Dial this on your mobile")[0]
    assert "**67*+15550100008#" in codes.body and "##002#" in codes.body
    assert codes.body.splitlines()[0] == "Turn on     **67*+15550100008#"
    assert all("KEYsaved" not in page_text(p) for p in ui.pages)


def test_back_in_the_forward_questions_never_returns_to_the_purchase():
    ui = ScriptedUI([
        *add_a_line("Telnyx"),
        ("Use the saved Telnyx key?", "Use the saved key"),
        ("A new number or one you already have?", "One I already have"),
        ("Which number?", "+15550100007"),
        ("Connect +15550100007 to ai-phone?", "Connect +15550100007"),
        ("Also forward your own mobile to it?", "Yes"),
        ("Your mobile number", "<back>"),
        ("Also forward your own mobile to it?", "No"),
        ("Everything works", "<quit>"),
    ])
    actions = FakeActions(exists=True, saved_keys={"TELNYX_API_KEY": "k"})
    flow.run(ui, actions)
    assert len(actions.args("attach_number")) == 1 and not actions.args("forward")
    assert ui.menu("Also forward")[3] is False  # the number is connected: there is no going back to it


def test_no_owned_numbers_offers_to_buy_one():
    ui = ScriptedUI([
        *add_a_line("Telnyx"),
        ("Your Telnyx credentials", ["KEYnew"]),
        ("A new number or one you already have?", "One I already have"),
        ("No numbers on this Telnyx account", "Buy a new number"),
        ("Where should the number be?", ["", ""]),
        ("Pick a number", "<quit>"),
    ])
    actions = FakeActions(exists=True, owned_numbers=[])
    flow.run(ui, actions)
    assert ui.questions("No numbers on this Telnyx account")[0].detail == \
        "Buy one instead, or add a number in the carrier's console and run setup again."
    assert ui.questions("A new number or one")[0].checks == [Check("Telnyx key", "works · 0 numbers on this account", True)]
    assert [(f.label, f.placeholder) for f in ui.fields[:1]] == [("API key", "KEY…")]


def test_numbers_already_connected_are_left_out():
    """Connecting a number twice would overwrite its routing, so numbers that are already a line are not offered."""
    ui = ScriptedUI([
        *add_a_line("Telnyx"),
        ("Use the saved Telnyx key?", "Use the saved key"),
        ("A new number or one you already have?", "One I already have"),
        ("Which number?", "+15550100008"),
        ("Connect +15550100008 to ai-phone?", "<quit>"),
    ])
    actions = FakeActions(exists=True, saved_keys={"TELNYX_API_KEY": "k"},
                          lines={"telnyx": {"kind": "trunk", "number": "+15550100007"}, "agent": SIP_LINE})
    flow.run(ui, actions)
    assert [o.label for o in ui.menu("Which number?")[1]] == ["+15550100008"]
    assert ui.questions("A new number or one")[0].checks == [Check("Telnyx key", "works · 2 numbers on this account", True)]

    ui = ScriptedUI([
        *add_a_line("Telnyx"),
        ("Use the saved Telnyx key?", "Use the saved key"),
        ("A new number or one you already have?", "One I already have"),
        ("No other numbers on this Telnyx account", "Buy a new number"),
        ("Where should the number be?", ["", ""]),
        ("Pick a number", "<quit>"),
    ])
    lines = {"telnyx": {"kind": "trunk", "number": "+15550100007"}, "telnyx-2": {"kind": "trunk", "number": "+15550100008"}}
    actions = FakeActions(exists=True, saved_keys={"TELNYX_API_KEY": "k"}, lines=lines)
    flow.run(ui, actions)
    page = ui.questions("No other numbers")[0]
    assert page.detail == "Buy one instead, or add a number in the carrier's console and run setup again."
    assert [o.label for o in ui.menu("No other numbers")[1]] == ["Buy a new number"]
    assert not actions.args("attach_number")


def test_bad_key_is_asked_again_with_the_error():
    ui = ScriptedUI([
        *add_a_line("Telnyx"),
        ("Your Telnyx credentials", ["KEYbad"]),
        ("Your Telnyx credentials", ["KEYgood"]),
        ("A new number or one you already have?", "<quit>"),
    ])
    actions = FakeActions(exists=True, owned_numbers=Seq(ActionError("That key did not work: 401 Unauthorized"), OWNED))
    assert flow.run(ui, actions).code == 1
    first, _busy, second, _busy2 = ui.questions("Your Telnyx credentials")
    assert first.error == "" and second.error == "That key did not work: 401 Unauthorized"
    assert actions.args("owned_numbers") == [("telnyx", {"TELNYX_API_KEY": "KEYbad"}), ("telnyx", {"TELNYX_API_KEY": "KEYgood"})]
    assert all("KEYbad" not in page_text(p) for p in ui.pages)


def test_a_saved_key_that_fails_asks_for_a_new_one():
    ui = ScriptedUI([
        *add_a_line("Twilio"),
        ("Use the saved Twilio key?", "Use the saved key"),
        ("Your Twilio credentials", ["ACsid", "tokSECRET"]),
        ("A new number or one you already have?", "<quit>"),
    ])
    old = {"TWILIO_ACCOUNT_SID": "ACold", "TWILIO_AUTH_TOKEN": "old"}
    actions = FakeActions(exists=True, saved_keys=old,
                          owned_numbers=Seq(ActionError("That key did not work: 401 Unauthorized"), OWNED))
    flow.run(ui, actions)
    assert ui.questions("Your Twilio credentials")[0].error == "That key did not work: 401 Unauthorized"
    assert actions.args("owned_numbers") == [("twilio", old), ("twilio", TWILIO)]


def test_enter_a_different_key_asks_for_it():
    ui = ScriptedUI([
        *add_a_line("Twilio"),
        ("Use the saved Twilio key?", "Enter a different key"),
        ("Your Twilio credentials", ["ACsid", "tokSECRET"]),
        ("A new number or one you already have?", "<quit>"),
    ])
    actions = FakeActions(exists=True, saved_keys={"TWILIO_ACCOUNT_SID": "ACold", "TWILIO_AUTH_TOKEN": "old"})
    flow.run(ui, actions)
    assert actions.args("owned_numbers") == [("twilio", TWILIO)]


def test_pending_number_shows_the_messages_and_skips_forwarding():
    messages = ["order pending", "Line twilio is written but pending. Resume with: ai-phone lines resume twilio --key-env <NAME>"]
    ui = ScriptedUI([
        *add_a_line(),
        ("Your Twilio credentials", ["ACsid", "tokSECRET"]),
        ("A new number or one you already have?", "A new number"),
        ("Where should the number be?", ["", "555"]),
        ("Pick a number", "+15550100002"),
        ("Buy +15550100002 for $1.15/month?", "Buy +15550100002"),
        ("The number is not ready yet", "Continue"),
        ("A few things need attention", "Continue"),
        FINISH,
    ])
    actions = FakeActions(exists=True, buy_number=LineResult("twilio", 2, messages),
                          check_lines=[("twilio", "registering", "")])
    assert flow.run(ui, actions).code == 0
    pending = ui.questions("The number is not ready yet")[0]
    assert pending.body == "\n".join(messages)
    assert ui.menu("The number is not ready yet")[1][0].label == "Continue"
    assert not ui.questions("Also forward") and not actions.args("forward")
    assert ui.questions("A few things")[0].checks[2] == Check("Line twilio", "registering", None)


def test_failed_number_says_something_went_wrong():
    ui = ScriptedUI([
        *add_a_line(),
        ("Your Twilio credentials", ["ACsid", "tokSECRET"]),
        ("A new number or one you already have?", "A new number"),
        ("Where should the number be?", ["", ""]),
        ("Pick a number", "+15550100001"),
        ("Buy +15550100001", "Buy +15550100001"),
        ("Something went wrong", "Continue"),
        ("A few things need attention", "<quit>"),
    ])
    actions = FakeActions(exists=True, buy_number=Seq(ActionError("could not save the key: disk full")))
    flow.run(ui, actions)
    assert ui.questions("Something went wrong")[0].body == "could not save the key: disk full"


def test_own_trunk():
    ui = ScriptedUI([
        ("Give your AI", "Add a line"),
        ("How should people reach your agent?", "My own SIP trunk"),
        ("Your SIP trunk", ["sip.example.com", "alex1234", "pwSECRET", "+15550100000"]),
        ("Which transport?", "TLS"),
        ("Your SIP trunk", ["", "", "pwSECRET2", ""]),
        ("Which transport?", "TLS"),
        ("Everything works", "Continue"),
        FINISH,
    ])
    bad = "config.lines.trunk.registrar: expected sip:host[:port][;transport=…]"
    actions = FakeActions(exists=True, add_trunk_line=Seq(ActionError(bad), "trunk"))
    assert flow.run(ui, actions).code == 0
    assert actions.args("add_trunk_line") == [("sip:sip.example.com", "alex1234", "pwSECRET", "+15550100000", "tls"),
                                              ("sip:sip.example.com", "alex1234", "pwSECRET2", "+15550100000", "tls")]
    retry = ui.questions("Your SIP trunk")[1]
    assert retry.error == bad
    assert [f.default for f in ui.fields[4:]] == ["sip.example.com", "alex1234", "", "+15550100000"]
    assert ui.fields[2].secret and not ui.fields[0].secret
    assert [(o.label, o.hint) for o in ui.menu("Which transport?")[1]] == [("UDP", ""), ("TCP", ""), ("TLS", "encrypted signalling")]
    assert all("pwSECRET" not in page_text(p) for p in ui.pages)


def test_sip_address_name_taken_is_asked_again():
    ui = ScriptedUI([
        ("Give your AI", "Add a line"),
        ("How should people reach your agent?", "A SIP address"),
        ("Name your SIP address", ["", ""]),
        ("Name your SIP address", ["desk", ""]),
        ("Everything works", "<quit>"),
    ])
    actions = FakeActions(exists=True, add_sip_line=Seq(ActionError("config.lines.agent.aor: expected sip:user@host[:port]"), "desk"))
    flow.run(ui, actions)
    assert actions.args("add_sip_line") == [("agent", "192.0.2.10"), ("desk", "192.0.2.10")]
    assert ui.questions("Name your SIP address")[1].error == "config.lines.agent.aor: expected sip:user@host[:port]"
    assert ui.questions("Name your SIP address")[0].detail == \
        "Softphones and other agents call sip:<name>@<this Mac>. Free; no carrier."


def test_validators():
    assert flow.required("  ") == "Enter a value." and flow.required("x") is None
    assert flow.e164("+15550100000") is None and flow.e164("5550100000") == "Use international format, like +15550100000."
    assert flow.country("us") is None and flow.country("USA") == "Use a two-letter country code, like US or GB."
    assert flow.area("") is None and flow.area("212") is None and flow.area("1") and flow.area("12a")
    assert flow.sip_name("agent-2") is None and flow.sip_name("Agent") == "Use lowercase letters, digits and dashes."
    assert flow.sip_name("a.b") and flow.sip_name("x" * 33)
    assert flow.host("192.0.2.10") is None and flow.host("mac.example.com") is None and flow.host("my mac")
    assert flow.url("https://api.example.com/v1") is None and flow.url("api.example.com")
    assert flow.speed("0.9") is None and flow.speed("2") is None
    assert flow.speed("2.5") == flow.speed("fast") == flow.speed("nan") == "Use a number from 0.5 to 2.0."
    assert flow.count("0") is None and flow.count("20") is None and flow.count("-1") and flow.count("1.5")


def test_price_size_and_plural_helpers():
    assert flow.price(Number("+1", 1.15, "USD", False, "")) == "$1.15/month"
    assert flow.price(Number("+1", 1.0, "GBP", False, "")) == "£1.00/month"
    assert flow.price(Number("+1", 1.0, "EUR", False, "")) == "€1.00/month"
    assert flow.price(Number("+1", 1.0, "XYZ", False, "")) == "1.00 XYZ/month"
    assert flow.price(Number("+1", None, None, False, "")) == "the carrier's monthly price"
    assert flow.gb(4_300_000_000) == "4.3 GB" and flow.gb(None) == "several GB"
    assert flow.plural(1, "number") == "1 number" and flow.plural(2, "number") == "2 numbers"


def test_back_at_the_first_question_goes_to_the_previous_step():
    ui = ScriptedUI([
        WELCOME_NEW,
        ("Where should your agent think?", "On this Mac"),
        ("Which model size?", "Qwen 9B"),
        ("How should people reach your agent?", "<back>"),
        ("Where should your agent think?", "On this Mac"),
        ("Which model size?", "Qwen 4B"),
        ("How should people reach your agent?", "Skip"),
        ("How should your agent sound?", "<quit>"),
    ])
    actions = FakeActions()
    assert flow.run(ui, actions).code == 1
    assert ui.steps == [WELCOME, BRAIN, LINE, BRAIN, LINE, VOICE]
    assert actions.args("save_brain_local") == [("t9",), ("t4",)]
    assert ui.menus[-3][2] == 1  # the size menu now defaults to what was saved


def test_back_later_in_a_step_restarts_it():
    ui = ScriptedUI([
        WELCOME_NEW,
        ("Where should your agent think?", "My own endpoint"),
        ("Your endpoint", "<back>"),
        ("Where should your agent think?", "On this Mac"),
        ("Which model size?", "<default>"),
        ("How should people reach your agent?", "<quit>"),
    ])
    actions = FakeActions()
    flow.run(ui, actions)
    assert ui.steps == [WELCOME, BRAIN, BRAIN, LINE]
    assert actions.changes() == ["save_brain_local"]


def test_back_from_the_first_step_returns_to_welcome():
    ui = ScriptedUI([
        WELCOME_NEW,
        ("Where should your agent think?", "<back>"),
        WELCOME_NEW,
        ("Where should your agent think?", "<quit>"),
    ])
    assert flow.run(ui, FakeActions()).code == 1
    assert ui.steps == [WELCOME, BRAIN, WELCOME, BRAIN]


def test_back_from_the_only_step_of_a_change_returns_to_welcome():
    ui = ScriptedUI([
        ("Give your AI", "Change what callers hear"),
        ("What should callers hear?", "<back>"),
        ("Give your AI", "Cancel"),
    ])
    actions = FakeActions(exists=True)
    assert flow.run(ui, actions).code == 1
    assert ui.steps == [WELCOME, CALLERS, WELCOME]
    assert not actions.changes()


def test_cancel_keeps_finished_steps_only():
    ui = ScriptedUI([
        WELCOME_NEW,
        ("Where should your agent think?", "On this Mac"),
        ("Which model size?", "<default>"),
        ("How should people reach your agent?", "A SIP address"),
        ("Name your SIP address", ["", ""]),
        ("How should your agent sound?", "<quit>"),
    ])
    actions = FakeActions()
    outcome = flow.run(ui, actions)
    assert outcome.code == 1 and ui.closed
    assert "Setup cancelled" in outcome.summary
    assert actions.changes() == ["save_brain_local", "add_sip_line"]


def test_leaving_on_the_done_page_still_finishes():
    ui = ScriptedUI([
        ("Give your AI", "Change what callers hear"),
        ("What should callers hear?", ["", "", "0"]),
        ("A few things need attention", "Continue"),
        ("You're set up", "<quit>"),
    ])
    actions = FakeActions(exists=True)
    outcome = flow.run(ui, actions)
    assert outcome.code == 0 and "uv run ai-phone run" in outcome.summary
    assert actions.args("save_callers") == [(GREETING, INSTRUCTIONS, 0)]
    assert ui.questions("A few things")[0].checks[2] == Check("Lines", "none yet · run setup again to add one", None)
    assert not actions.args("check_lines")  # nothing to check, so nothing is started


def test_callers_that_do_not_validate_are_asked_again():
    ui = ScriptedUI([
        ("Give your AI", "Change what callers hear"),
        ("What should callers hear?", ["x" * 501, "", ""]),
        ("What should callers hear?", ["Hello there.", "Take a message.", "5"]),
        ("A few things need attention", "<quit>"),
    ])
    too_long = "config.inbound.greeting: 1 to 500 characters"
    actions = FakeActions(exists=True, save_callers=Seq(ActionError(too_long), None))
    flow.run(ui, actions)
    assert ui.questions("What should callers hear?")[1].error == too_long
    assert actions.args("save_callers")[-1] == ("Hello there.", "Take a message.", 5)
    assert [f.placeholder for f in ui.fields[:3]] == ["", "", "0 means no limit"]


def test_existing_config_change_the_voice_only():
    ui = ScriptedUI([
        ("Give your AI", "Change the voice"),
        ("How should your agent sound?", "British · male"),
        ("British · male", "George"),
        ("George", "Hear it"),
        ("George", "Hear it"),
        ("George", "Use this voice"),
        ("How fast should it talk?", ["0.9"]),
        ("Everything works", "Continue"),
        FINISH,
    ])
    actions = FakeActions(exists=True, lines={"agent": SIP_LINE},
                          play_sample=Seq(ActionError("could not play the sample: afplay not found"), None, None))
    assert flow.run(ui, actions).code == 0
    assert ui.steps == [WELCOME, VOICE, CHECK, DONE]
    assert actions.changes() == ["save_voice"]
    assert actions.args("save_voice") == [("bm_george", 0.9)]
    assert actions.args("play_sample") == [("bm_george", 1.0, flow.SAMPLE)] * 2 + [("bm_george", 0.9, DISCLOSURE)]
    groups = ui.menu("How should your agent sound?")
    assert [(o.label, o.hint) for o in groups[1]] == [("American · female", "2 voices"), ("American · male", "1 voice"),
                                                      ("British · female", "1 voice"), ("British · male", "1 voice")]
    assert groups[2] == 0  # the current voice is American · female
    george = [p for p in ui.pages if p.question == "George"]
    assert [p.error for p in george] == ["", "", "could not play the sample: afplay not found", "", ""]
    assert "Playing…" in ui.labels
    assert [(o.label, o.hint) for o in ui.menu("George")[1]] == [
        ("Use this voice", ""), ("Hear it", "plays a short sample on this Mac's speakers")]
    assert "Voice    George · British · male · 0.9×" in ui.questions("You're set up")[0].body


def test_no_voices_offers_the_speech_model_download():
    ui = ScriptedUI([
        ("Give your AI", "Change the voice"),
        ("How should your agent sound?", "Download it now"),
        ("How should your agent sound?", "American · female"),
        ("American · female", "Heart"),
        ("Heart", "Use this voice"),
        ("How fast should it talk?", [""]),
        ("Everything works", "<quit>"),
    ])
    actions = FakeActions(exists=True, lines={"agent": SIP_LINE}, voices=Seq([], ["af_heart"], ["af_heart"]))
    flow.run(ui, actions)
    first = ui.pages[1]
    assert first.detail == "The speech model is not downloaded yet, so there are no voices to pick from."
    assert [(o.label, o.hint) for o in ui.menus[1][1]] == [
        ("Download it now", "about 0.4 GB"), ("Keep the default voice", "Heart · American · female")]
    assert len(actions.args("pull_voice_model")) == 1 and callable(actions.args("pull_voice_model")[0][0])
    assert ui.questions("Downloading the speech model")
    assert actions.args("save_voice") == [("af_heart", 1.0)]


def test_no_voices_keep_the_default():
    ui = ScriptedUI([
        ("Give your AI", "Change the voice"),
        ("How should your agent sound?", "Keep the default voice"),
        ("Everything works", "<quit>"),
    ])
    actions = FakeActions(exists=True, lines={"agent": SIP_LINE}, voices=[])
    flow.run(ui, actions)
    assert actions.args("save_voice") == [("af_heart", 1.0)]
    assert not actions.args("pull_voice_model")
    rows = ui.questions("Everything works")[0].checks
    assert [r.label for r in rows] == ["Models", "Config", "Line agent"]  # no speech model: no voice row


def test_no_voices_download_failure_keeps_the_default():
    ui = ScriptedUI([
        ("Give your AI", "Change the voice"),
        ("How should your agent sound?", "Download it now"),
        ("Everything works", "<quit>"),
    ])
    actions = FakeActions(exists=True, lines={"agent": SIP_LINE}, voices=[],
                          pull_voice_model=ActionError("could not download mlx-community/Kokoro-82M-bf16: OSError"))
    flow.run(ui, actions)
    assert actions.args("save_voice") == [("af_heart", 1.0)]


def test_check_offers_the_download_and_reports():
    ui = ScriptedUI([
        ("Give your AI", "Change what callers hear"),
        ("What should callers hear?", ["", "", ""]),
        ("Download the models now?", "Download now"),
        ("Everything works", "Check again"),
        ("Download the models now?", "Later"),
        ("A few things need attention", "Check again"),
        ("Download the models now?", "Download now"),
        ("A few things need attention", "Continue"),
        FINISH,
    ])
    failed = "could not download mlx-community/Qwen3.5-4B-MLX-4bit: ConnectError"
    actions = FakeActions(exists=True, missing=["mlx-community/Qwen3.5-4B-MLX-4bit"], lines={"agent": SIP_LINE},
                          pull_models=Seq(None, ActionError(failed)))
    assert flow.run(ui, actions).code == 0
    offer = ui.questions("Download the models now?")[0]
    assert offer.detail == ("About 4.3 GB into the Hugging Face cache (~/.cache/huggingface). "
                            "ai-phone needs them to answer calls.")
    assert [(o.label, o.hint) for o in ui.menu("Download the models now?")[1]] == [
        ("Download now", "4.3 GB"), ("Later", "run: uv run ai-phone models pull")]
    assert "Checking the download size…" in ui.labels
    downloading = ui.questions("Downloading the models")[0]
    assert downloading.detail == "You can leave this running. It picks up where it stopped if interrupted."
    assert all(callable(a[0]) for a in actions.args("pull_models")) and len(actions.args("pull_models")) == 2
    ok, later, failed_page = [p for p in ui.pages if p.step == CHECK and p.question in ("Everything works", "A few things need attention")]
    assert ok.checks[0] == Check("Models", "downloaded", True)
    assert later.checks[0] == Check("Models", "not downloaded yet · uv run ai-phone models pull", None)
    assert failed_page.checks[0] == Check("Models", failed, False)
    assert [(o.label) for o in ui.menu("Everything works")[1]] == ["Continue", "Check again"]


def test_endpoint_brain_check_lines_and_voice_rows():
    ui = ScriptedUI([
        ("Give your AI", "Change what callers hear"),
        ("What should callers hear?", ["", "", ""]),
        ("A few things need attention", "Continue"),
        FINISH,
    ])
    lines = {"main": {"kind": "trunk", "number": "+15550100001"}, "office": {"kind": "trunk", "number": "+15550100002"}}
    actions = FakeActions(exists=True, lines=lines,
                          check_lines=[("main", "registered", ""), ("office", "degraded", "403 Forbidden")])
    actions.cur.brain_kind, actions.cur.base_url, actions.cur.model = "openai_compatible", "https://api.example.com/v1", "qwen"
    outcome = flow.run(ui, actions)
    assert "Checking your lines… (up to 20 seconds)" in ui.labels and "Playing your agent's voice…" in ui.labels
    rows = ui.questions("A few things need attention")[0].checks
    assert rows == [Check("Models", "downloaded", True), Check("Config", "loads", True),
                    Check("Line main", "registered", True), Check("Line office", "degraded · 403 Forbidden", False),
                    Check("Voice", "Heart · played", True)]
    assert "Brain    https://api.example.com/v1 · qwen" in outcome.summary


def test_check_skips_lines_when_the_config_does_not_load():
    ui = ScriptedUI([
        ("Give your AI", "Change what callers hear"),
        ("What should callers hear?", ["", "", ""]),
        ("A few things need attention", "<quit>"),
    ])
    actions = FakeActions(exists=True, lines={"agent": SIP_LINE}, check_config="config.lines.agent.aor: expected sip:user@host",
                          play_sample=ActionError("could not play the sample: no audio device"))
    flow.run(ui, actions)
    rows = ui.questions("A few things need attention")[0].checks
    assert rows == [Check("Models", "downloaded", True), Check("Config", "config.lines.agent.aor: expected sip:user@host", False),
                    Check("Voice", "could not play the sample: no audio device", False)]
    assert not actions.args("check_lines")


def test_check_shows_a_skipped_row_without_the_sip_library():
    ui = ScriptedUI([
        ("Give your AI", "Change what callers hear"),
        ("What should callers hear?", ["", "", ""]),
        ("A few things need attention", "<quit>"),
    ])
    actions = FakeActions(exists=True, lines={"agent": SIP_LINE},
                          check_lines=[("lines", "skipped", "SIP library not built · run ./scripts/install.sh")])
    flow.run(ui, actions)
    assert ui.questions("A few things")[0].checks[2] == \
        Check("Lines", "skipped · SIP library not built · run ./scripts/install.sh", None)


def test_done_text_uses_tilde_paths(monkeypatch):
    monkeypatch.delenv("AI_PHONE_HOME", raising=False)
    actions = FakeActions(exists=True, lines={"agent": SIP_LINE, "twilio": NUMBER_LINE})
    text = flow.done_text(actions, flow.Session())
    assert str(Path.home()) not in text
    repo = Path(ai_phone.__file__).parents[2]
    shown = "~/" + str(repo.relative_to(Path.home())) if repo.is_relative_to(Path.home()) else str(repo)
    assert f"run from {shown}:" in text
    lines = text.splitlines()
    assert lines[:3] == ["Saved",
                         "  ~/.ai-phone/config.yaml     lines, brain, voice, what callers hear",
                         "  ~/.ai-phone/secrets.env     keys and passwords, readable only by you"]
    assert "Brain    on this Mac · Qwen 4B" in lines and "Voice    Heart · American · female · 1.0×" in lines
    assert any(ln.startswith("  agent") and "sip:agent@192.0.2.10" in ln and ln.endswith("sip") for ln in lines)
    assert any(ln.startswith("  twilio") and "+15550100001" in ln and "trunk · twilio" in ln for ln in lines)
    for section in ("Lines", "Start ai-phone", "Try it", "Give your AI the phone"):
        assert section in lines
    assert "  uv run ai-phone run" in lines
    assert "  pjsua --local-port 5070 sip:agent@127.0.0.1" in lines
    assert "  call +15550100001 from any phone (your normal call rates)" in lines
    assert 'Tell your AI: "You can make and take phone calls with the ai-phone command,' in text
    assert "`ai-phone hangup` ends a call.\"" in text


def test_done_text_without_lines():
    text = flow.done_text(FakeActions(exists=True), flow.Session())
    lines = text.splitlines()
    assert "  none yet · run setup again to add one" in lines
    assert not any("pjsua" in ln for ln in lines)
