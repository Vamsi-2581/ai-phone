"""What the setup flow needs from a front-end. The flow (flow.py) runs in its own thread and calls these methods;
the full-screen app (tui.py) and the plain prompts (plain.py) implement them."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, TypeVar

STEPS = ("Welcome", "Mode", "Brain", "Line", "Voice", "Settings", "Check", "Done")
T = TypeVar("T")


class Back(Exception):
    """Esc, or the Back option. `at_start` is True when it was raised at the first question of a step."""

    def __init__(self, at_start: bool = False):
        super().__init__("back")
        self.at_start = at_start


class Cancel(Exception):
    """q or Ctrl-C: leave setup. What earlier steps saved stays; nothing more is written."""


@dataclass
class Option:
    label: str
    hint: str = ""


@dataclass
class Check:
    label: str
    value: str
    ok: bool | None  # True: green tick; False: red cross; None: amber "not yet"


@dataclass
class Field:
    label: str
    default: str = ""
    secret: bool = False  # hidden input; the value is never drawn
    placeholder: str = ""
    validate: Callable[[str], str | None] | None = None  # the error to show, or None when the value is fine


@dataclass
class Page:
    step: int  # index into STEPS
    question: str
    detail: str = ""
    note: str = ""  # a dim third line (Welcome)
    checks: list[Check] = field(default_factory=list)
    body: str = ""  # a preformatted block shown as-is (dial codes, the summary)
    error: str = ""  # shown in red under the detail


@dataclass
class Outcome:
    code: int  # 0 finished, 1 cancelled, 3 failed
    summary: str = ""  # printed to the terminal after the full-screen app closes


class UI(Protocol):
    def begin(self, step: int) -> None: ...
    def choose(self, page: Page, options: list[Option], default: int = 0, back: bool = True) -> int: ...
    def ask(self, page: Page, fields: list[Field]) -> list[str]: ...
    def busy(self, page: Page, label: str, work: Callable[[], T]) -> T: ...
    def progress(self, page: Page, label: str, work: Callable[[Callable[[int, int], None]], T]) -> T: ...
    def close(self) -> None: ...


class BaseUI:
    """Counts the questions asked since the step began, so Back knows whether it is at the step's first question.

    `choose` shows a trailing "Back" option when `back` is True; choosing it (or Esc) raises `self.back()`.
    `ask` validates each field with `Field.validate` and only returns when every field is valid.
    `busy` shows `label` with a spinner while `work()` runs in the calling thread, and returns its result
    (exceptions propagate). `progress` does the same with a progress bar; `work` receives `report(done, total)`."""

    def __init__(self) -> None:
        self.asked = 0

    def begin(self, step: int) -> None:
        self.asked = 0

    def next_question(self) -> None:
        self.asked += 1

    def back(self) -> Back:
        return Back(at_start=self.asked <= 1)

    def close(self) -> None:
        pass
