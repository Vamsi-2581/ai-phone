"""Registered-time and recovery report from a daemon log (the 24 h soak gate)."""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

LINE = re.compile(r"^(\S+ \S+?),\d+ \w+ registration line=(\S+) state=(\S+) code=(\d+)")
END = re.compile(r"^(\S+ \S+?),\d+ \w+ soak end( \(daemon died\))?")


def analyse(path: Path, line_id: str) -> dict:
    t_reg_start = None
    registered = 0.0
    drops: list[dict] = []
    first = last = None
    died = False
    for raw in Path(path).read_text().splitlines():
        m = LINE.match(raw)
        e = END.match(raw)
        if not (m or e):
            continue
        try:
            stamp = datetime.strptime((m or e).group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue  # a registration line with an unparseable timestamp is ignored, never fatal
        if e:
            last = stamp
            died = bool(e.group(2))
            break
        if m.group(2) != line_id:
            continue  # other lines never move this line's clock
        first = first or stamp
        last = stamp
        state = m.group(3)
        if state == "registered" and t_reg_start is None:
            t_reg_start = stamp
            if drops and drops[-1]["recover_sec"] is None:
                drops[-1]["recover_sec"] = (stamp - drops[-1]["_at"]).total_seconds()
        elif state != "registered" and t_reg_start is not None:
            registered += (stamp - t_reg_start).total_seconds()
            t_reg_start = None
            drops.append({"at": stamp.strftime("%Y-%m-%d %H:%M:%S"), "recover_sec": None, "_at": stamp})
    if t_reg_start is not None and last is not None and not died:
        registered += (last - t_reg_start).total_seconds()  # a dead daemon earns nothing past its last registration event
    total = (last - first).total_seconds() if first and last else 0.0
    for d in drops:
        d.pop("_at", None)
    pct = (registered / total * 100) if total else 0.0

    def ok(max_recover_sec: float, min_registered_pct: float) -> bool:
        return (not died and pct >= min_registered_pct
                and all(d["recover_sec"] is not None and d["recover_sec"] <= max_recover_sec for d in drops))

    return {"registered_pct": pct, "total_sec": total, "drops": drops, "died": died, "ok": ok}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--line", default="main")
    ap.add_argument("--max-recover-sec", type=float, default=60)
    ap.add_argument("--min-registered-pct", type=float, default=99)
    a = ap.parse_args(argv)
    r = analyse(Path(a.log), a.line)
    print(f"line {a.line}: registered {r['registered_pct']:.2f}% of {r['total_sec'] / 3600:.1f} h; drops: {len(r['drops'])}"
          + ("; THE DAEMON DIED before the end" if r["died"] else ""))
    for d in r["drops"]:
        print(f"  {d['at']}  recovered in {d['recover_sec']} s")
    good = r["ok"](a.max_recover_sec, a.min_registered_pct)
    print("SOAK PASS" if good else "SOAK FAIL")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
