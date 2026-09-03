import json
import stat
from pathlib import Path

import pytest

from ai_phone.telephony.state import LineStatus, StateError, StateFile, next_state


def test_registration_transitions():
    assert next_state("unregistered", True, 200) == "registered"
    assert next_state("registered", False, 408) == "degraded"
    assert next_state("degraded", True, 200) == "registered"
    assert next_state("registered", False, 0) == "unregistered"   # we unregistered on purpose
    assert next_state("registering", False, 401) == "degraded"


def test_state_file_round_trip_is_private_and_atomic(tmp_path: Path):
    p = tmp_path / "state.json"
    sf = StateFile(p)
    lines = {"main": LineStatus("main", "trunk", "+442079460000", "registered", "sip.telnyx.com", 1700000300.0, None, 1700000000.0)}
    sf.write(lines, None, {"kind": "local", "base_url": None})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    d = StateFile.read(p)
    assert d["lines"]["main"]["state"] == "registered" and d["active_call"] is None and d["brain"]["kind"] == "local"
    assert not list(tmp_path.glob(".state-*"))  # no temp file left behind
    sf.write(lines, {"line_id": "main", "remote": "sip:+44…", "started_at": 1.0, "direction": "in"}, {"kind": "local", "base_url": None})
    assert StateFile.read(p)["active_call"]["line_id"] == "main"


def test_missing_state_file_raises(tmp_path: Path):
    with pytest.raises(StateError):
        StateFile.read(tmp_path / "none.json")
