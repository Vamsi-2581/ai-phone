"""`ai-phone setup`: the setup wizard (full-screen, or plain prompts when there is no terminal)."""
from __future__ import annotations

import os
import sys


def run_setup(plain: bool | None = None) -> int:
    from ai_phone import paths
    from ai_phone.setup import flow
    from ai_phone.setup.actions import Actions

    actions = Actions(paths.config_path())
    session = flow.Session()  # shared with the full-screen app, which may have to leave before the flow returns
    if plain or not (sys.stdin.isatty() and sys.stdout.isatty()):
        from ai_phone.setup.plain import PlainUI
        outcome = flow.run(PlainUI(), actions, session)
        if outcome.code != 0 and outcome.summary:  # a finished run already showed its summary on the Done page
            print(f"\n{outcome.summary}", flush=True)  # on a line of its own, after a prompt that ctrl+c interrupted
        if session.left_download:
            # huggingface_hub joins its download threads at exit, which would wait for the whole download; the files
            # so far stay in the cache and the next pull resumes from them
            sys.stderr.flush()
            os._exit(1)
        return outcome.code
    from ai_phone.setup.tui import run_tui
    outcome = run_tui(lambda ui: flow.run(ui, actions, session), summary=lambda: flow.cancel_text(session))
    if outcome.summary:
        print(outcome.summary)
    return outcome.code
