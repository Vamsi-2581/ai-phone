"""The setup flow as plain numbered prompts: for pipes, scripts and `ai-phone setup --plain`. No colour, no escape
codes; secrets are read with getpass and never echoed."""
from __future__ import annotations

import getpass

from ai_phone.setup.ui import STEPS, BaseUI, Cancel, Page

MARKS = {True: "✓", False: "✗", None: "○"}


def spaced(text: str) -> str:
    """'STEP 3 OF 7' -> 'S T E P  3  O F  7'."""
    return "  ".join(" ".join(word) for word in text.split())


def _read(read, prompt: str) -> str:
    try:
        return read(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Cancel() from None


class PlainUI(BaseUI):
    def show(self, page: Page) -> None:
        print()
        if page.step == 0:
            print("ai-phone setup")
        else:
            print(f"{spaced(f'STEP {page.step + 1} OF {len(STEPS)}')}  ·  {spaced(STEPS[page.step].upper())}")
        print(page.question)
        for text in (page.detail, page.note):
            if text:
                print(text)
        if page.error:
            print(f"! {page.error}")
        width = max((len(c.label) for c in page.checks), default=0)
        for c in page.checks:
            print(f"  {MARKS[c.ok]} {c.label.ljust(width)}   {c.value}")
        for line in page.body.splitlines():
            print(f"  {line}".rstrip())

    def choose(self, page, options, default=0, back=True):
        self.next_question()
        self.show(page)
        width = max(len(o.label) for o in options)
        for i, o in enumerate(options, 1):
            print(f"  {i}) {o.label.ljust(width)}   {o.hint}".rstrip())
        if back:
            print("  b) Back")
        retry = ("Answer with a number from the list, b to go back or q to quit." if back
                 else "Answer with a number from the list or q to quit.")
        on_enter = "b" if default == len(options) else str(default + 1)  # a confirmation defaults to Back
        while True:
            answer = _read(input, f"Choose [{on_enter}]: ").strip().lower() or on_enter  # Enter answers what [ ] shows
            if answer.isdecimal() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            if answer == "b" and back:
                raise self.back()
            if answer == "q":
                raise Cancel()
            print(retry)

    def ask(self, page, fields):
        self.next_question()
        self.show(page)
        values = []
        for f in fields:
            # the placeholder says what an empty answer means ("optional", "leave empty to keep the saved key")
            hint = f" [{f.default}]" if f.default and not f.secret else f" ({f.placeholder})" if f.placeholder else ""
            while True:
                if f.secret:
                    value = _read(getpass.getpass, f"{f.label}{hint}: ")
                else:
                    value = _read(input, f"{f.label}{hint}: ")
                value = (value if f.secret else value.strip()) or f.default  # a password may start or end with a space
                error = f.validate(value) if f.validate else None
                if error is None:
                    values.append(value)
                    break
                print(f"! {error}")
        return values

    def busy(self, page, label, work):
        print(label)
        return work()

    def progress(self, page, label, work):
        print(label)
        shown = -1

        def report(done: int, total: int) -> None:
            nonlocal shown
            if not total:
                return
            pct = min(100, max(0, done * 100 // total))
            if pct // 5 > shown:
                shown = pct // 5
                print(f"  {pct}%")

        return work(report)
