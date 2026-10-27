import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "latency_report.py"


def report(tmp_path, rows, *args):
    path = tmp_path / "turns.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return subprocess.run([sys.executable, str(SCRIPT), str(path), *args],
                          capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("latencies, expected_code, expected_p50, expected_p95", [
    ([800, 900, 950, 990], 0, 950, 990),
    ([800, 900, 1000, 1100], 1, 1000, 1100),
])
def test_gate_keeps_the_existing_quantile_and_latency_threshold(
    tmp_path, latencies, expected_code, expected_p50, expected_p95,
):
    result = report(tmp_path, [{"first_audible_ms": value} for value in latencies], "--expect", "4")
    assert result.returncode == expected_code, result.stderr
    assert f"p50={expected_p50:7.1f}  p95={expected_p95:7.1f}  n=4" in result.stdout
    assert f"GATE: {'PASS' if expected_code == 0 else 'FAIL'}" in result.stdout


@pytest.mark.parametrize("counters", [[0, 3, 3, 8, 34], [34, 0, 2]])
def test_ring_counters_show_the_latest_snapshot_including_a_reset(tmp_path, counters):
    rows = [{"first_audible_ms": 800, "tx_underruns": value, "rx_overruns": value + 1}
            for value in counters]
    result = report(tmp_path, rows)
    assert result.returncode == 0, result.stderr
    assert (f"latest cumulative ring counters: tx={counters[-1]} rx={counters[-1] + 1}"
            " (last selected row)") in result.stdout


@pytest.mark.parametrize("bad_row", [
    {},
    {"first_audible_ms": None},
    {"first_audible_ms": "900"},
    {"first_audible_ms": float("nan")},
    {"first_audible_ms": float("inf")},
    {"first_audible_ms": -float("inf")},
    {"first_audible_ms": 0},
    {"first_audible_ms": -1},
    {"first_audible_ms": True},
    {"first_audible_ms": {}},
])
def test_twenty_complete_rows_need_twenty_valid_measurements(tmp_path, bad_row):
    rows = [{"first_audible_ms": 800} for _ in range(19)] + [bad_row]
    result = report(tmp_path, rows, "--expect", "20")
    assert result.returncode == 1, result.stderr
    assert "turns: 20" in result.stdout
    assert "invalid or missing first_audible_ms in 1 complete turn(s)" in result.stdout
    assert "GATE: FAIL" in result.stdout
    assert "Traceback" not in result.stderr


def test_last_selects_rows_before_checking_measurements(tmp_path):
    rows = [{"first_audible_ms": None, "tx_underruns": 99}]
    rows += [{"first_audible_ms": 800, "tx_underruns": 3, "rx_overruns": 0} for _ in range(20)]
    result = report(tmp_path, rows, "--last", "20", "--expect", "20")
    assert result.returncode == 0, result.stderr
    assert "turns: 20" in result.stdout
    assert "latest cumulative ring counters: tx=3 rx=0 (last selected row)" in result.stdout
    assert "GATE: PASS" in result.stdout


def test_latest_counters_include_a_final_incomplete_turn(tmp_path):
    rows = [{"first_audible_ms": 800, "tx_underruns": 3, "rx_overruns": 2},
            {"incomplete": True, "first_audible_ms": None, "tx_underruns": 9, "rx_overruns": 7}]
    result = report(tmp_path, rows, "--expect", "1")
    assert result.returncode == 0, result.stderr
    assert "turns: 1   incomplete (caller hung up mid-turn): 1" in result.stdout
    assert "latest cumulative ring counters: tx=9 rx=7 (last selected row)" in result.stdout
    assert "GATE: PASS" in result.stdout


def test_incomplete_turns_do_not_satisfy_the_expected_sample_count(tmp_path):
    rows = [{"first_audible_ms": 800}, {"incomplete": True, "first_audible_ms": None}]
    result = report(tmp_path, rows, "--expect", "2")
    assert result.returncode == 1, result.stderr
    assert "only 1 complete turns, expected 2" in result.stdout
    assert "GATE: FAIL" in result.stdout


def test_an_empty_log_fails_without_inventing_counter_values(tmp_path):
    result = report(tmp_path, [], "--expect", "20")
    assert result.returncode == 1, result.stderr
    assert "latest cumulative ring counters: tx=unavailable rx=unavailable" in result.stdout
    assert "GATE: FAIL" in result.stdout
