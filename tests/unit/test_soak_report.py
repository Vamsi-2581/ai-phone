from pathlib import Path

from scripts.soak_report import analyse

LOG = """2026-09-10 12:00:00,000 INFO registration line=main state=registered code=200 reason=OK
2026-09-10 12:10:00,000 INFO registration line=main state=degraded code=408 reason=Request Timeout
2026-09-10 12:10:20,000 INFO registration line=main state=registered code=200 reason=OK
2026-09-10 12:30:00,000 INFO registration line=other state=degraded code=503 reason=x
2026-09-10 13:00:00,000 INFO soak end
"""


def test_analyse_counts_drops_and_recovery(tmp_path: Path):
    p = tmp_path / "soak.log"
    p.write_text(LOG)
    r = analyse(p, "main")
    assert r["drops"] == [{"at": "2026-09-10 12:10:00", "recover_sec": 20.0}]
    assert abs(r["registered_pct"] - (3580 / 3600 * 100)) < 0.01
    assert r["ok"](max_recover_sec=60, min_registered_pct=99)
    assert not r["ok"](max_recover_sec=10, min_registered_pct=99)


def test_other_lines_before_the_target_do_not_inflate_the_denominator(tmp_path: Path):
    log = ("2026-09-10 11:00:00,000 INFO registration line=other state=registered code=200 reason=OK\n"
           "2026-09-10 12:00:00,000 INFO registration line=main state=registered code=200 reason=OK\n"
           "2026-09-10 13:00:00,000 INFO soak end\n")
    p = tmp_path / "s.log"
    p.write_text(log)
    r = analyse(p, "main")
    assert r["total_sec"] == 3600 and abs(r["registered_pct"] - 100.0) < 0.01


def test_unrecovered_drop_fails_and_cli_exit_codes(tmp_path: Path, capsys):
    from scripts.soak_report import main
    p = tmp_path / "s.log"
    p.write_text("2026-09-10 12:00:00,000 INFO registration line=main state=registered code=200 reason=OK\n"
                 "2026-09-10 12:50:00,000 INFO registration line=main state=degraded code=408 reason=x\n"
                 "2026-09-10 13:00:00,000 INFO soak end\n")
    r = analyse(p, "main")
    assert r["drops"] == [{"at": "2026-09-10 12:50:00", "recover_sec": None}] and not r["ok"](60, 50)
    assert main([str(p), "--line", "main"]) == 1 and "SOAK FAIL" in capsys.readouterr().out
    p.write_text("2026-09-10 12:00:00,000 INFO registration line=main state=registered code=200 reason=OK\n"
                 "2026-09-10 13:00:00,000 INFO soak end\n")
    assert main([str(p), "--line", "main"]) == 0 and "SOAK PASS" in capsys.readouterr().out


def test_empty_and_malformed_logs_fail_cleanly(tmp_path: Path):
    p = tmp_path / "s.log"
    p.write_text("garbage\nmore garbage\n")
    r = analyse(p, "main")
    assert r["total_sec"] == 0 and r["registered_pct"] == 0 and not r["ok"](60, 99)


def test_bad_timestamp_and_empty_log_are_ignored_not_fatal(tmp_path: Path):
    p = tmp_path / "s.log"
    p.write_text("")
    r = analyse(p, "main")
    assert r["total_sec"] == 0 and r["drops"] == [] and not r["ok"](60, 99)
    p.write_text("2026-13-45 99:99:99,000 INFO registration line=main state=registered code=200 reason=OK\n"
                 "2026-09-10 12:00:00,000 INFO registration line=main state=registered code=200 reason=OK\n"
                 "2026-09-10 13:00:00,000 INFO soak end\n")
    r = analyse(p, "main")
    assert r["total_sec"] == 3600 and r["ok"](60, 99)
