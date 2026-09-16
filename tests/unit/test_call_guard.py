import datetime
import json
import os

import pytest

from ai_phone.call_guard import DailyCounter, is_emergency


@pytest.mark.parametrize("target", ["911", "+1911", "999", "+44999", "112", "+33112", "000", "+61000", "sip:911@example.org",
                                    "+49 110", "1-1-2",
                                    # separators the dialler accepts in a sip: user part
                                    "sip:9_1_1@carrier", "sip:++911@carrier", "sip:9+11@carrier", "sip:+1_911@carrier",
                                    # an international access prefix (00 or 011) before the country code
                                    "sip:0044999@carrier", "sip:01144999@carrier", "0044999",
                                    # the spec's rule also covers these, even though PBXs use them as extensions
                                    "100", "1000"])
def test_emergency_numbers_are_blocked(target):
    assert is_emergency(target)


@pytest.mark.parametrize("target", ["+15551234567", "+447700900123", "+19115551234", "sip:agents@127.0.0.1",
                                    "sip:peer@127.0.0.1:5062", "+44999123456", "", "sip:room101@hotel"])
def test_ordinary_targets_pass(target):
    assert not is_emergency(target)


def test_daily_counter_survives_a_restart_and_resets_the_next_day(tmp_path):
    day = [datetime.date(2026, 9, 16)]
    path = tmp_path / "calls-today.json"
    c = DailyCounter(path, limit=2, today=lambda: day[0])
    assert c.allowed() and c.record() == 1 and c.allowed() and c.record() == 2
    assert not c.allowed()
    assert not DailyCounter(path, limit=2, today=lambda: day[0]).allowed()   # a new daemon sees the same count
    day[0] = datetime.date(2026, 9, 17)
    assert c.allowed() and c.record() == 1


def test_zero_limit_means_unlimited_and_bad_files_count_as_zero(tmp_path):
    path = tmp_path / "calls-today.json"
    path.write_text("not json")
    c = DailyCounter(path, limit=0)
    assert c.allowed()
    for _ in range(5):
        c.record()
    assert c.allowed()
    assert DailyCounter(path, limit=1)._read() == 5


@pytest.mark.parametrize("count", ["-50", "2.0", "Infinity", '"2"', "true", "null"])
def test_a_count_that_is_not_a_non_negative_whole_number_reads_as_zero(tmp_path, count):
    path = tmp_path / "calls-today.json"
    path.write_text(f'{{"date": "2026-09-16", "count": {count}}}')
    c = DailyCounter(path, limit=1, today=lambda: datetime.date(2026, 9, 16))
    assert c._read() == 0 and c.allowed()
    assert c.record() == 1 and not c.allowed()


def test_record_reads_the_date_once(tmp_path):
    path = tmp_path / "calls-today.json"
    path.write_text('{"date": "2026-09-16", "count": 1}')
    days = iter([datetime.date(2026, 9, 16), datetime.date(2026, 9, 17)])   # midnight passes during record()
    c = DailyCounter(path, limit=5, today=lambda: next(days))
    assert c.record() == 2
    assert json.loads(path.read_text()) == {"date": "2026-09-16", "count": 2}


def test_record_syncs_the_new_file_before_replacing_the_old(tmp_path, monkeypatch):
    from ai_phone import call_guard
    order = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        order.append("fsync")
        real_fsync(fd)

    def replace(src, dst):
        order.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(call_guard.os, "fsync", fsync)
    monkeypatch.setattr(call_guard.os, "replace", replace)
    assert DailyCounter(tmp_path / "calls-today.json", limit=1).record() == 1
    assert order == ["fsync", "replace"]
