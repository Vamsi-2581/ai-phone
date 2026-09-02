"""ai-phone command line: `ai-phone run [--config PATH] [--lan] [--tier t9|t4] [--fake]`."""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from ai_phone.config import load_config
from ai_phone.daemon import Daemon


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ai-phone")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start the daemon in the foreground")
    r.add_argument("--config", default="~/.ai-phone/config.yaml")
    r.add_argument("--lan", action="store_true", help="accept calls from the LAN, loopback and link-local (dev/test)")
    r.add_argument("--tier", choices=["t9", "t4"], default="t9")
    r.add_argument("--fake", action="store_true", help="fake model backends (tests)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(a.config).expanduser())
    asyncio.run(Daemon(cfg, a.tier, Path(cfg.log.dir), a.lan, a.fake).run())
    return 0
