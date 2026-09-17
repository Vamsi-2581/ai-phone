"""The full-screen setup wizard, driven headless by Textual's pilot with small scripted flows."""
import asyncio
import threading
import time

import pytest
from rich.color import ColorType
from textual.widgets import Input, ProgressBar

from ai_phone.setup import look, tui
from ai_phone.setup.tui import PageScreen, SetupApp, run_tui
from ai_phone.setup.ui import Back, Cancel, Check, Field, Option, Outcome, Page

LINE = Page(2, "How should people reach your agent?", "detail")
OPTIONS = [Option("A phone number", "hint"), Option("A SIP address")]
WELCOME = Page(0, "Give your AI a phone number", "Calls answered and placed by models running on this Mac.",
               note="Picks a line, a brain and a voice, then checks it all works.",
               checks=[Check("SIP library", "built", True), Check("Memory", "16 GB", None),
                       Check("Models", "not downloaded yet", False)])


def screen_text(app) -> str:
    """What the terminal shows, one line per row (each row is a few strips of segments)."""
    rows = app.screen._compositor.render_full_update().strips
    return "\n".join("".join(seg.text for strip in row for seg in strip) for row in rows)


async def wait_for(pilot, app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting; the screen shows:\n{screen_text(app)}")
        await pilot.pause(0.01)


def segments(app):
    for row in app.screen._compositor.render_full_update().strips:
        for strip in row:
            yield from strip


def colour_of(app, text: str) -> str | None:
    """The foreground colour of the first drawn segment that contains `text`."""
    for seg in segments(app):
        if text in seg.text and seg.style and seg.style.color:
            return seg.style.color.get_truecolor().hex
    return None


def showing(app, text: str):
    """A predicate: a question screen is up and shows `text`."""
    return lambda: isinstance(app.screen, PageScreen) and app.screen.is_mounted and text in screen_text(app)


@pytest.mark.asyncio
async def test_choose_renders_and_returns_the_pick():
    got = {}

    def flow(ui):
        got["answer"] = ui.choose(LINE, OPTIONS)
        return Outcome(0)

    app = SetupApp(flow)
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "[3] Back"))
        text = screen_text(app)
        for part in ("✓ Welcome", "● Line", "○ Voice", "S T E P   3   O F   7", "ai-phone setup",
                     "How should people reach your agent?", "[1] A phone number", "hint", "[2] A SIP address",
                     "esc back"):
            assert part in text
        await pilot.press("down", "enter")
        await wait_for(pilot, app, lambda: app.outcome is not None)
    assert got["answer"] == 1
    assert app.outcome == Outcome(0)


@pytest.mark.asyncio
async def test_digits_jump_and_escape_goes_back():
    got = {}

    def pick(ui):
        got["pick"] = ui.choose(LINE, OPTIONS)
        return Outcome(0)

    app = SetupApp(pick)
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "[3] Back"))
        await pilot.press("2")
        await wait_for(pilot, app, lambda: app.outcome is not None)
    assert got["pick"] == 1

    backs = []

    def go_back(ui):
        ui.begin(2)
        for question in ("First", "Back option"):
            try:
                ui.choose(Page(2, question), OPTIONS)
            except Back as e:
                backs.append(e.at_start)
        ui.begin(2)
        ui.choose(Page(2, "Earlier"), OPTIONS)
        try:
            ui.choose(Page(2, "Later"), OPTIONS)
        except Back as e:
            backs.append(e.at_start)
        try:
            ui.ask(Page(2, "Your Telnyx credentials"), [Field("API key", secret=True)])
        except Back as e:
            backs.append(e.at_start)
        return Outcome(0)

    app = SetupApp(go_back)
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "First"))
        await pilot.press("escape")
        await wait_for(pilot, app, showing(app, "Back option"))
        await pilot.press("3")  # the trailing Back option
        await wait_for(pilot, app, showing(app, "Earlier"))
        await pilot.press("enter")
        await wait_for(pilot, app, showing(app, "Later"))
        await pilot.press("escape")
        await wait_for(pilot, app, showing(app, "API key"))
        await pilot.press("escape")  # a form goes back too, even while its field has focus
        await wait_for(pilot, app, lambda: app.outcome is not None)
    # asked counts up since begin(): the Back option is the step's second question, so it is not at the start
    assert backs == [True, False, False, False]


@pytest.mark.asyncio
async def test_the_default_option_starts_selected():
    got = {}

    def flow(ui):
        got["answer"] = ui.choose(LINE, OPTIONS, default=1)
        return Outcome(0)

    app = SetupApp(flow)
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "▸ [2] A SIP address"))
        await pilot.press("enter")
        await wait_for(pilot, app, lambda: app.outcome is not None)
    assert got["answer"] == 1


@pytest.mark.asyncio
async def test_q_cancels():
    seen = []

    def flow(ui):
        try:
            ui.choose(LINE, OPTIONS)
        except Cancel:
            seen.append("cancel")
            raise
        return Outcome(0)

    app = SetupApp(flow)
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "[3] Back"))
        await pilot.press("q")
        await wait_for(pilot, app, lambda: app.outcome is not None)
    assert seen == ["cancel"]
    assert app.outcome.code == 1


@pytest.mark.asyncio
async def test_form_hides_secrets_and_validates():
    got = {}
    fields = [Field("Account SID", secret=True, placeholder="AC…"),
              Field("Name", validate=lambda v: None if v == "agent" else "Use the name agent.")]

    def flow(ui):
        got["values"] = ui.ask(Page(2, "Your Twilio credentials", "Saved to ~/.ai-phone/secrets.env."), fields)
        return Outcome(0)

    app = SetupApp(flow)
    async with app.run_test(size=(104, 40)) as pilot:
        seen = []

        async def press(*keys):
            for key in keys:
                await pilot.press(key)
                seen.append(screen_text(app))

        await wait_for(pilot, app, showing(app, "Account SID"))
        first, second = app.screen.query(Input)
        assert app.focused is first
        assert "⏎ next field   esc back   ctrl+c quit" in screen_text(app)
        await press(*"ACsecret123", "enter")
        assert app.focused is second
        await press(*"bad", "enter")
        await wait_for(pilot, app, lambda: "Use the name agent." in screen_text(app))
        assert colour_of(app, "Use the name agent.") == look.RED
        assert app.focused is second and "values" not in got
        await press("q")  # a letter in a field, not quit
        assert second.value == "badq" and app.outcome is None
        app.screen.set_focus(None)
        await press("q")  # not even when no field has focus
        assert app.outcome is None and not app.screen.answered
        second.focus()
        second.clear()
        await press(*"agent", "enter")
        await wait_for(pilot, app, lambda: app.outcome is not None)
        seen.append(screen_text(app))
    assert got["values"] == ["ACsecret123", "agent"]
    assert seen and not any("ACsecret123" in t or "secret123" in t for t in seen)


@pytest.mark.asyncio
async def test_busy_spins_then_returns():
    release = threading.Event()
    got = {}

    def work():
        release.wait(5)
        return 7

    def flow(ui):
        got["result"] = ui.busy(Page(2, "Your Twilio credentials"), "Checking your Twilio account…", work)
        return Outcome(0)

    def spinner(app) -> str:
        line = next((l for l in screen_text(app).splitlines() if "Checking your Twilio account…" in l), "")
        return line.strip()[:1]

    app = SetupApp(flow)
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, lambda: spinner(app) in tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"))
        assert app.screen.busy == "Checking your Twilio account…"
        assert "ctrl+c quit" in screen_text(app) and "esc back" not in screen_text(app)
        assert colour_of(app, "Checking your Twilio account…") == look.MID
        frame = spinner(app)
        await wait_for(pilot, app, lambda: spinner(app) != frame)  # it turns
        assert "result" not in got
        release.set()
        await wait_for(pilot, app, lambda: app.outcome is not None)
    assert got["result"] == 7


@pytest.mark.asyncio
async def test_progress_bar_moves():
    release = threading.Event()
    got = {}

    def work(report):
        report(5, 10)
        report(10, 10)
        release.wait(5)
        return "pulled"

    def flow(ui):
        got["result"] = ui.progress(Page(1, "Where should your agent think?"), "Downloading models", work)
        return Outcome(0)

    def bar(app):
        bars = app.screen.query(ProgressBar) if isinstance(app.screen, PageScreen) else []
        return bars[0] if bars else None

    app = SetupApp(flow)
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, lambda: bar(app) is not None and bar(app).progress == 10)
        assert bar(app).total == 10
        await wait_for(pilot, app, lambda: "Downloading models  0.0 of 0.0 GB" in screen_text(app))
        release.set()
        await wait_for(pilot, app, lambda: app.outcome is not None)
    assert got["result"] == "pulled"


async def welcome_screen(width: int) -> str:
    app = SetupApp(lambda ui: Outcome(ui.choose(WELCOME, [Option("Continue"), Option("Cancel")], back=False)))
    async with app.run_test(size=(width, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "[2] Cancel"))
        text = screen_text(app)
        await pilot.press("enter")
        await wait_for(pilot, app, lambda: app.outcome is not None)
    return text


@pytest.mark.asyncio
async def test_welcome_shows_the_wordmark_when_wide_and_a_title_when_narrow():
    wide = await welcome_screen(104)
    assert "▀" in wide or "█" in wide
    assert "● Welcome ←" in wide and "○ Done" in wide
    for part in ("Give your AI a phone number", "Picks a line, a brain and a voice", "SIP library", "not downloaded yet",
                 "✓", "✗", "○"):
        assert part in wide
    assert "esc back" not in wide and "[3]" not in wide  # no Back on Welcome

    narrow = await welcome_screen(70)
    lines = [line.strip() for line in narrow.splitlines()]
    assert "ai-phone" in lines
    assert not any(block in narrow for block in "▀▄█")
    assert "● Welcome  1/7" in narrow and "○ Brain" not in narrow


@pytest.mark.asyncio
async def test_resizing_the_terminal_refits_the_crumbs_and_the_wordmark():
    app = SetupApp(lambda ui: Outcome(ui.choose(WELCOME, [Option("Continue")], back=False)))
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "○ Brain"))
        await pilot.resize_terminal(70, 40)
        await wait_for(pilot, app, lambda: "● Welcome  1/7" in screen_text(app))
        assert "█" not in screen_text(app) and "ai-phone" in [l.strip() for l in screen_text(app).splitlines()]
        await pilot.resize_terminal(104, 40)
        await wait_for(pilot, app, lambda: "○ Brain" in screen_text(app) and "█" in screen_text(app))
        await pilot.press("enter")
        await wait_for(pilot, app, lambda: app.outcome is not None)


@pytest.mark.asyncio
async def test_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    app = SetupApp(lambda ui: Outcome(ui.choose(WELCOME, [Option("Continue")], back=False)))
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "[1] Continue"))
        drawn = [seg for seg in segments(app) if seg.text.strip()]
        await pilot.press("enter")
        await wait_for(pilot, app, lambda: app.outcome is not None)
    assert drawn
    assert not [seg for seg in drawn if seg.style and seg.style.color and seg.style.color.type == ColorType.TRUECOLOR]

    # with no colours to tell it apart, the cursor in a field is drawn in reverse video
    app = SetupApp(lambda ui: Outcome(0, ui.ask(Page(2, "Your Telnyx credentials"), [Field("API key")])[0]))
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "API key"))
        await pilot.press("k", "e", "y")
        cursor = [seg for seg in segments(app) if seg.style and seg.style.reverse]
        await pilot.press("enter")
        await wait_for(pilot, app, lambda: app.outcome is not None)
    assert cursor and app.outcome == Outcome(0, "key")


@pytest.mark.asyncio
async def test_ctrl_c_on_a_question_cancels_and_exits():
    seen = []

    def flow(ui):
        try:
            ui.ask(Page(2, "Your Telnyx credentials"), [Field("API key", secret=True)])
        except Cancel:
            seen.append("cancel")
            raise
        return Outcome(0)

    app = SetupApp(flow)
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "API key"))
        assert isinstance(app.focused, Input)  # the key reaches the app even while a field has focus
        await pilot.press("ctrl+c")
        await wait_for(pilot, app, lambda: app.outcome is not None)
        await wait_for(pilot, app, lambda: app.return_code is not None)
    assert seen == ["cancel"]
    assert app.outcome == Outcome(1, "Setup cancelled.")


@pytest.mark.asyncio
async def test_ctrl_c_while_working_leaves_at_once():
    release = threading.Event()

    def flow(ui):
        ui.busy(Page(3, "Which voice?"), "Loading voices…", lambda: release.wait(5))
        ui.choose(Page(3, "Which voice?"), OPTIONS)  # never shown: the person already asked to quit
        return Outcome(0)

    app = SetupApp(flow)
    async with app.run_test(size=(104, 40)) as pilot:
        await wait_for(pilot, app, showing(app, "Loading voices…"))
        await pilot.press("ctrl+c")
        await wait_for(pilot, app, lambda: app.return_code is not None)
        assert app.quit_requested and app.outcome is None
    # the work was left to finish on its own; the flow then stops at its next question
    release.set()
    deadline = time.monotonic() + 5
    while app.outcome is None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert app.outcome == Outcome(1, "Setup cancelled.")


@pytest.mark.asyncio
async def test_run_tui_returns_the_flow_outcome():
    app = SetupApp(lambda ui: Outcome(0, "done"))
    async with app.run_test() as pilot:
        await wait_for(pilot, app, lambda: app.outcome is not None)
    assert app.outcome == Outcome(0, "done")


def test_run_tui_reports_a_crash_without_its_message(monkeypatch):
    class Headless(SetupApp):
        def run(self, **kw):
            return super().run(headless=True, **kw)

    def crash(ui):
        raise RuntimeError("token AC123 rejected")

    monkeypatch.setattr(tui, "SetupApp", Headless)
    assert run_tui(lambda ui: Outcome(0, "done")) == Outcome(0, "done")
    assert run_tui(crash) == Outcome(3, "setup stopped: RuntimeError")
