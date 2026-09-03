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
