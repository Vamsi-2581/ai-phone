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


def test_owned_numbers():
    from ai_phone.providers.fake import OWNED
    assert [(n.e164, n.source_id) for n in FakeProvider().owned_numbers()] == [(OWNED, "owned-1")]


def test_attach_connects_the_owned_number_and_buys_nothing(tmp_path: Path):
    p = FakeProvider()
    n = p.owned_numbers()[0]
    j = Journal.open(tmp_path, "main", "fake")
    r = p.attach(j, n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "ready" and j.state == "ready" and r.line["number"] == n.e164 and r.line["caller_id"] == n.e164
    assert r.line["user"] == "aimain1234" and r.line["provider"]["resources"] == {"brought_number_id": "owned-1", "profile_id": "profile-1",
                                                                                  "connection_id": "connection-1"}
    assert "number" not in p.carrier and "number" not in p.created
    assert p.carrier["owned"] == {"id": "owned-1", "name": n.e164, "connection": "connection-1"}
    assert j.resources()["number_brought"] is True and j.steps_done() == ["claim", "profile", "connection"]
    assert p.poll(j).line["number"] == n.e164


def test_attach_refuses_a_number_not_on_the_account(tmp_path: Path):
    from ai_phone.providers.base import Number
    p = FakeProvider()
    with pytest.raises(ProviderError, match="not on this fake account"):
        p.attach(Journal.open(tmp_path, "main", "fake"), Number("+442079460001", None, None, False, "x"), "main", "aimain1234", "pw")
    assert p.carrier == {} and p.created == {}


def test_attach_crash_is_reconciled(tmp_path: Path):
    p = FakeProvider(crash_after="connection")
    n = p.owned_numbers()[0]
    with pytest.raises(ProviderError, match="crash"):
        p.attach(Journal.open(tmp_path, "main", "fake"), n, "main", "aimain1234", "pw")
    p.crash_after = None
    assert p.attach(Journal.open(tmp_path, "main"), n, "main", "aimain1234", "pw").state == "ready"
    assert p.created == {"profile": 1, "connection": 1}


def test_release_of_a_brought_number_keeps_it(tmp_path: Path):
    p = FakeProvider()
    j = Journal.open(tmp_path, "main", "fake")
    p.attach(j, p.owned_numbers()[0], "main", "aimain1234", "pw")
    assert p.release(j).ok
    assert set(p.carrier) == {"owned"} and p.carrier["owned"]["connection"] is None


def test_a_buy_then_a_bring_in_one_journal_releases_the_bought_number(tmp_path: Path):
    p = FakeProvider()
    j = Journal.open(tmp_path, "main", "fake")
    p.provision(j, p.search_numbers("GB", None, 1)[0], "main", "aimain1234", "pw")
    p.attach(j, p.owned_numbers()[0], "main", "aimain1234", "pw")
    assert j.resources()["number_id"] == "number-1" and j.resources()["brought_number_id"] == "owned-1"
    assert p.release(j).ok
    assert set(p.carrier) == {"owned"} and p.carrier["owned"]["connection"] is None
