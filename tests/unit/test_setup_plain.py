"""Plain numbered prompts: what is printed, how answers are read, and that a secret is never echoed."""
from __future__ import annotations

import sys
import types

import pytest

from ai_phone.setup import run_setup
from ai_phone.setup.plain import PlainUI
from ai_phone.setup.ui import Back, Cancel, Check, Field, Option, Outcome, Page
from tests.unit.test_setup_flow import FakeActions

LINE_PAGE = Page(2, "How should people reach your agent?", "You can add more lines later by running setup again.",
                 checks=[Check("Twilio key", "works · 2 numbers on this account", True)])
LINE_OPTIONS = [Option("A phone number", "Twilio or Telnyx · a new number or one you already have"),
                Option("A SIP address", "free · softphones and other agents")]


@pytest.fixture
def typed(monkeypatch):
    """Scripted answers for input() and getpass.getpass(); the prompts are recorded. input() echoes its prompt like
    the real one does; getpass echoes nothing of what is typed."""
    answers: list = []
    prompts: list[str] = []

    def take(prompt, echo):
        prompts.append(prompt)
        if echo:
            print(prompt, end="")
        assert answers, f"no answer left for {prompt!r}"
        value = answers.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("builtins.input", lambda prompt="": take(prompt, True))
    monkeypatch.setattr("getpass.getpass", lambda prompt="Password: ", stream=None: take(prompt, False))
    return types.SimpleNamespace(answers=answers, prompts=prompts)


def test_choose_prints_the_page_and_enter_takes_the_default(typed, capsys):
    typed.answers.append("")
    assert PlainUI().choose(LINE_PAGE, LINE_OPTIONS, default=1) == 1
    assert capsys.readouterr().out.splitlines() == [
        "",
        "S T E P  3  O F  7  ·  L I N E",
        "How should people reach your agent?",
        "You can add more lines later by running setup again.",
        "  ✓ Twilio key   works · 2 numbers on this account",
        "  1) A phone number   Twilio or Telnyx · a new number or one you already have",
        "  2) A SIP address    free · softphones and other agents",
        "  b) Back",
        "Choose [2]: ",
    ]


def test_welcome_page_has_a_title_note_and_no_back(typed, capsys):
    typed.answers += ["b", "2"]
    page = Page(0, "Give your AI a phone number", "Calls answered and placed by models running on this Mac.",
                note="Picks a line, a brain and a voice, then checks it all works.",
                checks=[Check("SIP library", "not built yet · run ./scripts/install.sh", False),
                        Check("Memory", "8 GB · 4B model recommended", None)])
    assert PlainUI().choose(page, [Option("Continue"), Option("Cancel", "exit setup, nothing is saved")], back=False) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[:9] == [
        "",
        "ai-phone setup",
        "Give your AI a phone number",
        "Calls answered and placed by models running on this Mac.",
        "Picks a line, a brain and a voice, then checks it all works.",
        "  ✗ SIP library   not built yet · run ./scripts/install.sh",
        "  ○ Memory        8 GB · 4B model recommended",
        "  1) Continue",
        "  2) Cancel     exit setup, nothing is saved",
    ]
    assert "  b) Back" not in out
    assert out[-2] == "Choose [1]: Answer with a number from the list or q to quit."  # piped input echoes no newline


def test_b_goes_back_and_knows_whether_it_is_the_first_question(typed):
    ui = PlainUI()
    ui.begin(2)
    typed.answers.append("b")
    with pytest.raises(Back) as first:
        ui.choose(LINE_PAGE, LINE_OPTIONS)
    assert first.value.at_start
    ui.begin(2)
    typed.answers += ["1", "B"]
    ui.choose(LINE_PAGE, LINE_OPTIONS)
    with pytest.raises(Back) as later:
        ui.choose(LINE_PAGE, LINE_OPTIONS)
    assert not later.value.at_start


def test_q_quits(typed):
    typed.answers.append("q")
    with pytest.raises(Cancel):
        PlainUI().choose(LINE_PAGE, LINE_OPTIONS)


@pytest.mark.parametrize("stop", [EOFError(), KeyboardInterrupt()])
def test_end_of_input_or_ctrl_c_quits(typed, stop):
    typed.answers.append(stop)
    with pytest.raises(Cancel):
        PlainUI().choose(LINE_PAGE, LINE_OPTIONS)
    typed.answers.append(stop)
    with pytest.raises(Cancel):
        PlainUI().ask(LINE_PAGE, [Field("Name")])
    typed.answers.append(stop)
    with pytest.raises(Cancel):
        PlainUI().ask(LINE_PAGE, [Field("API key", secret=True)])


def test_an_answer_that_is_not_on_the_list_is_asked_again(typed, capsys):
    typed.answers += ["9", "x", "0", " 2 "]
    assert PlainUI().choose(LINE_PAGE, LINE_OPTIONS) == 1
    out = capsys.readouterr().out
    assert out.count("Answer with a number from the list, b to go back or q to quit.") == 3
    assert typed.prompts == ["Choose [1]: "] * 4


def test_ask_takes_defaults_and_asks_again_until_valid(typed, capsys):
    typed.answers += ["", "Agent", "desk", "192.0.2.10", "5"]
    must_be_lower = lambda v: None if v.islower() else "Use lowercase letters, digits and dashes."  # noqa: E731
    fields = [Field("Name", "agent", validate=must_be_lower), Field("Desk", validate=must_be_lower),
              Field("This Mac's address", "", placeholder="192.0.2.1"), Field("Outbound calls per day", "20")]
    assert PlainUI().ask(Page(2, "Name your SIP address"), fields) == ["agent", "desk", "192.0.2.10", "5"]
    assert typed.prompts == ["Name [agent]: ", "Desk: ", "Desk: ", "This Mac's address (192.0.2.1): ",
                             "Outbound calls per day [20]: "]
    assert capsys.readouterr().out.count("! Use lowercase letters, digits and dashes.\n") == 1


def test_a_secret_is_read_hidden_and_never_printed(typed, capsys):
    typed.answers += ["", "sk-live-SECRET", "  sk-live-SECRET  "]
    required = lambda v: None if v else "Enter a value."  # noqa: E731
    page = Page(1, "Your endpoint", "Any server that speaks the OpenAI chat API.")
    ui = PlainUI()
    assert ui.ask(page, [Field("API key", secret=True, validate=required)]) == ["sk-live-SECRET"]
    assert ui.ask(page, [Field("API key", secret=True, placeholder="leave empty for none")]) == ["sk-live-SECRET"]
    out = capsys.readouterr()
    assert "SECRET" not in out.out and "SECRET" not in out.err
    assert typed.prompts == ["API key: ", "API key: ", "API key (leave empty for none): "]


def test_errors_and_bodies_are_printed(typed, capsys):
    typed.answers.append("")
    page = Page(2, "Dial this on your mobile", "Open the phone app, type the code and press call.",
                error="could not reach 127.0.0.1: ConnectError", body="Turn on     **67*+15550100001#\nCheck       *#67#")
    PlainUI().choose(page, [Option("Done")], back=False)
    out = capsys.readouterr().out.splitlines()
    assert out[3:7] == ["Open the phone app, type the code and press call.",
                        "! could not reach 127.0.0.1: ConnectError",
                        "  Turn on     **67*+15550100001#",
                        "  Check       *#67#"]


def test_busy_prints_the_label_and_returns_the_result(capsys):
    ui = PlainUI()
    assert ui.busy(LINE_PAGE, "Checking your Twilio account…", lambda: 42) == 42
    assert capsys.readouterr().out == "Checking your Twilio account…\n"
    with pytest.raises(RuntimeError):
        ui.busy(LINE_PAGE, "Playing…", lambda: (_ for _ in ()).throw(RuntimeError("boom")))


def test_progress_prints_at_most_once_per_five_percent(capsys):
    def work(report):
        for done in (0, 1, 2, 3, 5, 9, 10, 42, 43, 44, 100, 100):
            report(done, 100)
        report(5, 0)  # an unknown total prints nothing
        return "ok"

    assert PlainUI().progress(LINE_PAGE, "Downloading…", work) == "ok"
    assert capsys.readouterr().out.splitlines() == ["Downloading…", "  0%", "  5%", "  10%", "  42%", "  100%"]


def sip_setup_answers():
    return [
        "",   # Welcome: Continue
        "",   # Brain: On this Mac
        "",   # size: Qwen 4B
        "2",  # Line: A SIP address
        "", "",  # name and address: defaults
        "", "", "",  # voice group, voice, Use this voice
        "",   # speed
        "", "", "",  # greeting, instructions, daily limit
        "",   # Check: Continue
        "",   # Done: Finish
    ]


def test_run_setup_plain_end_to_end(typed, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    made = []
    monkeypatch.setattr("ai_phone.setup.actions.Actions", lambda path: made.append(path) or made_actions)
    made_actions = FakeActions()
    typed.answers += sip_setup_answers()
    assert run_setup(plain=True) == 0
    assert made == [tmp_path / "config.yaml"]
    assert made_actions.changes() == ["save_brain_local", "add_sip_line", "save_voice", "save_callers"]
    out = capsys.readouterr().out
    assert out.count("uv run ai-phone run") == 1  # the summary is the Done page's body, printed once
    assert "  pjsua --local-port 5070 sip:agent@127.0.0.1" in out
    assert out.splitlines()[-2:] == ["  1) Finish", "Choose [1]: "]
    assert not typed.answers


def test_run_setup_without_a_terminal_uses_plain_prompts(typed, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    monkeypatch.setattr("ai_phone.setup.actions.Actions", lambda path: FakeActions())
    monkeypatch.setattr("ai_phone.setup.sys", types.SimpleNamespace(stdin=Terminal(False), stdout=Terminal(True)))
    typed.answers.append("q")
    assert run_setup() == 1
    out = capsys.readouterr().out
    assert "ai-phone setup" in out
    assert out.endswith("Setup cancelled. What earlier steps saved is kept; nothing else was changed.\n")


class Terminal:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def test_run_setup_in_a_terminal_runs_the_full_screen_app(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    actions = FakeActions()
    monkeypatch.setattr("ai_phone.setup.actions.Actions", lambda path: actions)
    monkeypatch.setattr("ai_phone.setup.sys", types.SimpleNamespace(stdin=Terminal(True), stdout=Terminal(True)))
    seen = []

    def run_tui(flow_fn):
        seen.append(flow_fn)
        return Outcome(0, "Saved\n  summary")

    monkeypatch.setitem(sys.modules, "ai_phone.setup.tui", types.SimpleNamespace(run_tui=run_tui))
    assert run_setup() == 0
    assert len(seen) == 1 and callable(seen[0])
    assert capsys.readouterr().out == "Saved\n  summary\n"
