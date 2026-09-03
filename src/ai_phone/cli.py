"""ai-phone command line. `run` lives here; `lines`, `status` and `call` register from their own modules."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from ai_phone import cli_call, cli_lines, cli_status, paths
from ai_phone.config import load_config


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ai-phone")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start the daemon in the foreground")
    r.add_argument("--config", default=None, help=f"default: {paths.config_path()}")
    r.add_argument("--lan", action="store_true", help="every line accepts calls from the LAN, loopback and link-local (dev/test)")
    r.add_argument("--tier", choices=["t9", "t4"], default="t4")
    r.add_argument("--fake", action="store_true", help="fake model backends (tests)")
    cli_lines.register(sub)
    cli_status.register(sub)
    cli_call.register(sub)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.cmd == "run":
        from ai_phone.daemon import Daemon  # models and PJSIP load only for `run`
        cfg = load_config(Path(a.config).expanduser() if a.config else paths.config_path())
        for w in cfg.warnings:
            logging.getLogger("ai-phone").warning(w)
        asyncio.run(Daemon(cfg, a.tier, Path(cfg.log.dir), a.lan, a.fake).run())
        return 0
    try:
        return int(a.func(a))
    except SystemExit as e:  # subcommands use SystemExit(<code>) for fatal user errors
        return int(e.code or 0)
