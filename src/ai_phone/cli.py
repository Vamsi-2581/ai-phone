"""ai-phone command line. `run` and `setup` live here; `lines`, `status`, `call` and `models` register from their own modules."""
from __future__ import annotations

import argparse
import sys
import asyncio
import logging
from pathlib import Path

from ai_phone import cli_call, cli_lines, cli_models, cli_status, models, paths
from ai_phone.config import load_config


def _setup(a) -> int:
    from ai_phone.setup import run_setup  # Textual loads only for `setup`
    return run_setup(plain=True if a.plain else None)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ai-phone")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start the daemon in the foreground")
    r.add_argument("--config", default=None, help=f"default: {paths.config_path()}")
    r.add_argument("--lan", action="store_true", help="every line accepts calls from the LAN, loopback and link-local (dev/test)")
    r.add_argument("--tier", choices=["t9", "t4"], default=None, help="default: models.tier from the config (auto picks by memory)")
    r.add_argument("--fake", action="store_true", help="fake model backends (tests)")
    cli_lines.register(sub)
    cli_status.register(sub)
    cli_call.register(sub)
    cli_models.register(sub)
    st = sub.add_parser("setup", help="the setup wizard: a line, a brain, a voice, then a check")
    st.add_argument("--plain", action="store_true", help="numbered prompts instead of the full-screen wizard")
    st.set_defaults(func=_setup)
    a = p.parse_args(argv)
    if a.cmd == "run":
        # only the daemon logs at INFO: elsewhere httpx would print a line per Hub or carrier request over the command's own
        # output. Other commands keep Python's default (warnings only, to the current stderr, which the wizard captures).
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        import os
        from ai_phone.daemon import Daemon  # models and PJSIP load only for `run`
        from ai_phone.secrets import load_secrets
        cfg = load_config(Path(a.config).expanduser() if a.config else paths.config_path(), env={**os.environ, **load_secrets(paths.secrets_path())})
        for w in cfg.warnings:
            logging.getLogger("ai-phone").warning(w)
        tier = a.tier or models.resolve_tier(cfg.models.tier)
        asyncio.run(Daemon(cfg, tier, Path(cfg.log.dir), a.lan, a.fake).run())
        return 0
    try:
        return int(a.func(a))
    except SystemExit as e:  # subcommands use SystemExit(<code>) for fatal user errors and SystemExit("message") for a bare one
        if e.code is None or isinstance(e.code, int):
            return e.code or 0
        print(str(e.code), file=sys.stderr)
        return 2
