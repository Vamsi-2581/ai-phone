"""`ai-phone setup`: the setup wizard (full-screen, or plain prompts when there is no terminal)."""
from __future__ import annotations

import sys


def run_setup(plain: bool | None = None) -> int:
    from ai_phone import paths
    from ai_phone.setup import flow
    from ai_phone.setup.actions import Actions

    actions = Actions(paths.config_path())
    if plain or not (sys.stdin.isatty() and sys.stdout.isatty()):
        from ai_phone.setup.plain import PlainUI
        outcome = flow.run(PlainUI(), actions)
        if outcome.code != 0 and outcome.summary:  # a finished run already showed its summary on the Done page
            print(outcome.summary)
        return outcome.code
    from ai_phone.setup.tui import run_tui
    outcome = run_tui(lambda ui: flow.run(ui, actions))
    if outcome.summary:
        print(outcome.summary)
    return outcome.code
