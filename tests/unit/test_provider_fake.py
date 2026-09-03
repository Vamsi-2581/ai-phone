from pathlib import Path

import pytest

from ai_phone.lines.journal import Journal
from ai_phone.providers import load_provider
from ai_phone.providers.base import ProviderError
from ai_phone.providers.fake import FakeProvider


def test_registry_loads_fake():
    p = load_provider("fake", key="k")
    assert isinstance(p, FakeProvider) and p.name == "fake" and p.signalling_cidrs


def test_provision_release_round_trip(tmp_path: Path):
    p = FakeProvider()
    n = p.search_numbers("GB", "020", 3)[0]
    j = Journal.open(tmp_path, "main", "fake")
    r = p.provision(j, n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "ready" and r.line["registrar"] == "sip:sip.fake.invalid" and r.line["user"] == "aimain1234"
    assert r.line["number"] == n.e164 and r.line["caller_id"] == n.e164 and set(r.line["provider"]["resources"]) == {"profile_id", "connection_id", "number_id"}
    assert set(p.carrier) == {"profile", "connection", "number"} and j.steps_done() == ["profile", "connection", "number"]
    assert p.release(j).ok and p.carrier == {}


def test_crash_between_api_and_journal_does_not_duplicate(tmp_path: Path):
    p = FakeProvider(crash_after="connection")
    n = p.search_numbers("GB", None, 1)[0]
    j = Journal.open(tmp_path, "main", "fake")
    with pytest.raises(ProviderError, match="crash"):
        p.provision(j, n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert p.created["connection"] == 1
    p.crash_after = None
    r = p.provision(Journal.open(tmp_path, "main"), n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "ready" and p.created["connection"] == 1  # reconciled by name, not created twice


def test_pending_then_poll(tmp_path: Path):
    p = FakeProvider(pending_polls=2)
    n = p.search_numbers("GB", None, 1)[0]
    j = Journal.open(tmp_path, "main", "fake")
    assert p.provision(j, n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd").state == "pending"
    assert p.poll(j).state == "pending"
    assert p.poll(j).state == "ready"


def test_caller_id_verification():
    p = FakeProvider()
    step = p.start_caller_id_verification("+447700900123")
    assert step.kind == "enter_code_in_cli"
    assert p.complete_caller_id_verification(step, "000000") is False
    assert p.complete_caller_id_verification(step, "123456") is True
