"""No unit test may run the real `claude` or `codex`.

Those two spend the person's own Claude or ChatGPT plan: a turn is real money and a shared resource, and a suite that
quietly uses one is also a suite that fails on a machine where nobody is logged in. A green run has to mean the fakes
were used, so an attempt to start one of them from tests/unit fails here, loudly, saying what to do instead.

A fake installed under the real name is fine, and that is the point: what is refused is one of those names from
anywhere outside the test's own temporary directory. tests/integration is deliberately not covered -- two of its tests
exist to drive the real CLIs once.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REAL_CLIS = {"claude", "codex", "claude.exe", "codex.exe"}


@pytest.fixture(autouse=True)
def _no_real_subscription_cli(request, monkeypatch, tmp_path_factory):
    if "tests/unit" not in Path(str(request.node.fspath)).as_posix():
        return
    ours = tmp_path_factory.getbasetemp().resolve()

    def refuse(argv) -> None:
        try:
            first = argv if isinstance(argv, (str, bytes)) else argv[0]
        except (IndexError, TypeError):
            return
        path = Path(str(first))
        if path.name not in REAL_CLIS:
            return
        try:
            if path.resolve().is_relative_to(ours):
                return  # a fake this test installed under that name
        except OSError:
            pass
        raise AssertionError(
            f"a unit test tried to run {str(first)!r}. That spends a turn of the owner's plan and fails on a machine "
            f"with no login: install the fake CLI (tests/unit/test_cli_brain.FAKE_CLI) under that name on an isolated "
            f"PATH, or pass its path to the backend."
        )

    for name in ("Popen", "run"):
        real = getattr(subprocess, name)

        def guarded(args, *a, _real=real, **kw):
            refuse(args)
            return _real(args, *a, **kw)

        monkeypatch.setattr(subprocess, name, guarded)
