"""The full-screen front-end of `ai-phone setup`. The flow runs in a daemon thread; each question is a screen, and
the answer comes back to the flow through a queue."""
from __future__ import annotations

import os
import queue
import threading
from typing import Callable

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import Input, ProgressBar, Rule, Static

from ai_phone.setup import look
from ai_phone.setup.ui import UI, BaseUI, Cancel, Option, Outcome, Page

_BACK, _QUIT = object(), object()
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class PageScreen(Screen):
    """One Page: a menu (options), a form (fields), or a spinner/progress bar while the flow works."""

    def __init__(self, page: Page, reply=None, options=(), default=0, back=False, fields=(), busy="", progress=False):
        super().__init__()
        self.page, self.reply = page, reply
        self.options, self.default, self.back = list(options), default, back
        self.fields, self.busy, self.progress = list(fields), busy, progress
        self.answered = False
        self.frame, self.label, self.spinning = 0, busy, None

    def compose(self) -> ComposeResult:
        page, width = self.page, self.app.size.width
        with Vertical(id="page"):
            yield Static(look.crumbs(page.step, width), id="crumbs")
            yield Static(Text("ai-phone setup", style=Style(color=look.WHITE, bold=True)), id="title")
            yield Rule(id="rule")
            if page.step == 0:
                yield Static(self._logo(width), id="logo")
                yield Static(Text(page.question, style=Style(color=look.WHITE, bold=True)), classes="centre")
                yield Static(Text(page.detail, style=Style(color=look.MID)), classes="centre")
                yield Static(Text(page.note, style=Style(color=look.DIM)), id="note")
            else:
                yield Static(look.eyebrow(page.step), classes="eyebrow")
                yield Static(Text(page.question, style=Style(color=look.WHITE, bold=True)), classes="question")
                yield Static(Text(page.detail, style=Style(color=look.MID)), classes="detail")
            if page.checks:
                with Center():
                    yield Static(look.checks_text(page.checks), id="checks")
            if page.body:
                with Center():
                    yield Static(Text(page.body, style=Style(color=look.WHITE), no_wrap=True), id="body")
            if page.error:
                yield Static(Text(page.error), classes="error")
            if self.options:
                with Center():
                    yield look.Menu([(o.label, o.hint) for o in self.options], index=self.default, id="menu")
            elif self.fields:
                with Center(), Vertical(id="form"):
                    for field in self.fields:
                        yield Static(Text(field.label, style=Style(color=look.DIM)), classes="label")
                        yield Input(value=field.default, placeholder=field.placeholder, password=field.secret)
                    yield Static(Text(), id="status")
            elif self.busy:
                with Center(), Vertical(id="work"):
                    yield Static(self._spinner(), id="status")
                    if self.progress:
                        yield ProgressBar(total=None, show_eta=False)
            with Center():
                yield Static(look.hint(self._keys()), id="hint")

    def on_mount(self) -> None:
        if self.fields:
            self.query(Input).first().focus()
        if self.busy:
            self.spinning = self.set_interval(0.08, self._spin)

    def on_resize(self, event: events.Resize) -> None:
        width = event.size.width
        self.query_one("#crumbs", Static).update(look.crumbs(self.page.step, width))
        if self.page.step == 0:
            self.query_one("#logo", Static).update(self._logo(width))

    def _logo(self, width: int) -> Text:
        if width < look.WORDMARK_MIN_WIDTH:
            return Text("ai-phone", style=Style(color=look.WHITE, bold=True))
        return look.wordmark()

    def _keys(self) -> str:
        if self.busy:
            return look.BUSY_KEYS
        if self.fields:
            return look.FORM_KEYS
        return look.MENU_KEYS if self.back else look.MENU_KEYS.replace("esc back   ", "")

    def _spinner(self) -> Text:
        t = Text(FRAMES[self.frame % len(FRAMES)] + " ", style=Style(color=look.ACCENT))
        t.append(self.label, style=Style(color=look.MID))
        return t

    def _spin(self) -> None:
        self.frame += 1
        self.query_one("#status", Static).update(self._spinner())

    def hold(self, label: str) -> None:
        """Keep this answered form up while the flow works on its values: the fields turn read-only (the one that
        had focus keeps its border) and the spinner runs in the form's status line."""
        if self.focused is not None:
            self.focused.add_class("held")
        self.busy = self.label = label
        self.query(Input).set(disabled=True)
        self.query_one("#hint", Static).update(look.hint(self._keys()))
        self.query_one("#status", Static).update(self._spinner())
        if self.spinning is None:
            self.spinning = self.set_interval(0.08, self._spin)

    def set_progress(self, done: int, total: int) -> None:
        if total:
            self.label = f"{self.busy}  {done / 1e9:.1f} of {total / 1e9:.1f} GB"
            self.query_one(ProgressBar).update(total=total, progress=done)
        self.query_one("#status", Static).update(self._spinner())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        inputs = list(self.query(Input))
        i = inputs.index(event.input)
        last = i == len(inputs) - 1
        # Enter checks its own field; on the last one, every field must be fine before the flow gets the values
        for j in [i, *range(len(inputs))] if last else [i]:
            check = self.fields[j].validate
            problem = check(inputs[j].value) if check else None
            if problem:
                inputs[j].focus()
                self.query_one("#status", Static).update(Text(problem, style=Style(color=look.RED)))
                return
        self.query_one("#status", Static).update(Text())
        if last:
            self.send([field_input.value for field_input in inputs])
        else:
            inputs[i + 1].focus()

    def send(self, answer) -> None:
        """Answer the flow, once; keys pressed after that are ignored."""
        if self.reply is None or self.answered:
            return
        self.answered = True
        self.reply(answer)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and (self.back or self.fields):
            self.send(_BACK)
        elif event.key == "q" and not self.fields:  # in a form, q is a letter
            self.send(_QUIT)
        elif self.options:
            m = self.query_one(look.Menu)
            if event.key in ("down", "j"):
                m.index = (m.index + 1) % len(m.options)
            elif event.key in ("up", "k"):
                m.index = (m.index - 1) % len(m.options)
            elif event.key == "enter":
                self.send(m.index)
            elif event.character and event.character.isdigit() and 1 <= int(event.character) <= len(m.options):
                m.index = int(event.character) - 1
                self.send(m.index)


class TextualUI(BaseUI):
    def __init__(self, app: SetupApp):
        super().__init__()
        self.app = app
        self.form: PageScreen | None = None  # the form on screen whose values the flow has just been given

    def _call(self, fn, *args):
        if self.app.quit_requested:
            raise Cancel()
        return self.app.call_from_thread(fn, *args)

    def _show(self, screen: PageScreen) -> None:
        self.form = None
        self._call(self.app.show, screen)

    def _wait(self, screen: PageScreen):
        q: queue.Queue = queue.Queue()
        screen.reply = q.put
        self._show(screen)
        answer = q.get()
        if answer is _QUIT:
            raise Cancel()
        return answer

    def choose(self, page, options, default=0, back=True):
        self.next_question()
        shown = list(options) + ([Option("Back")] if back else [])
        answer = self._wait(PageScreen(page, options=shown, default=default, back=back))
        if answer is _BACK or (back and answer == len(options)):
            raise self.back()
        return answer

    def ask(self, page, fields):
        self.next_question()
        screen = PageScreen(page, fields=fields)
        answer = self._wait(screen)
        if answer is _BACK:
            raise self.back()
        self.form = screen
        return answer

    def busy(self, page, label, work):
        form = self.form
        if form is not None and (form.page.step, form.page.question) == (page.step, page.question):
            self._call(form.hold, label)  # checking what was just typed: the form stays, as in the approved look
        else:
            self._show(PageScreen(page, busy=label))
        return work()

    def progress(self, page, label, work):
        screen = PageScreen(page, busy=label, progress=True)
        self._show(screen)
        return work(lambda done, total: self.app.call_from_thread(screen.set_progress, done, total))


class SetupApp(App):
    CSS = look.CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+c,ctrl+q", "request_quit", "quit", priority=True)]

    def __init__(self, flow_fn: Callable[[UI], Outcome]):
        # Under NO_COLOR Textual drops every colour only in ANSI mode; otherwise it redraws them in grey
        super().__init__(ansi_color=True if "NO_COLOR" in os.environ else None)
        self.flow_fn, self.outcome, self.quit_requested = flow_fn, None, False

    def on_mount(self) -> None:
        self.ui = TextualUI(self)
        threading.Thread(target=self._run_flow, daemon=True, name="setup-flow").start()

    def _run_flow(self) -> None:
        try:
            self.outcome = self.flow_fn(self.ui)
        except Cancel:
            self.outcome = Outcome(1, "Setup cancelled.")
        except Exception as e:  # the flow's own errors are ActionErrors it already handled; this is a bug
            self.outcome = Outcome(3, f"setup stopped: {type(e).__name__}")
        finally:
            if self.return_code is None:  # not already closed by ctrl+c while the flow was working
                try:
                    self.call_from_thread(self.exit)
                except RuntimeError:  # the app finished in the meantime
                    pass

    def show(self, screen: PageScreen):
        # returning the mount lets the flow thread (in call_from_thread) wait until the screen is up
        if isinstance(self.screen, PageScreen):
            return self.switch_screen(screen)
        return self.push_screen(screen)

    def action_request_quit(self) -> None:
        # On a question screen: quit now (the flow thread gets Cancel). While the flow is working (a purchase, a
        # download): the work finishes or is abandoned with the process; a purchase is journaled either way.
        self.quit_requested = True
        if isinstance(self.screen, PageScreen) and self.screen.reply is not None and not self.screen.busy:
            self.screen.send(_QUIT)
        else:
            self.exit()


def run_tui(flow_fn: Callable[[UI], Outcome]) -> Outcome:
    app = SetupApp(flow_fn)
    app.run()
    return app.outcome or Outcome(1, "Setup cancelled.")
