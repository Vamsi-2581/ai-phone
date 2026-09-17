import json
from pathlib import Path

import httpx
import pytest

from ai_phone.lines.journal import Journal
from ai_phone.providers.base import Number, ProviderError
from ai_phone.providers.telnyx import SIGNALLING_CIDRS, TELNYX_API, TelnyxProvider


class Carrier:
    """Just enough of api.telnyx.com/v2 to provision, poll, release and verify."""

    def __init__(self, order_status="success", crash_on=None):
        self.profiles, self.connections, self.orders, self.numbers, self.verified = {}, {}, {}, {}, {}
        self.order_status, self.crash_on = order_status, crash_on
        self.calls = []
        self.patches = []  # (number id, body) of every PATCH to a number

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        assert req.headers["authorization"] == "Bearer KEY"
        body = json.loads(req.content) if req.content else {}
        p, m = req.url.path, req.method
        if m == "GET" and p == "/v2/available_phone_numbers":
            return httpx.Response(200, json={"data": [{"phone_number": "+442079460000", "cost_information": {"monthly_cost": "1.00", "currency": "GBP"}, "requirements": []}]})
        if m == "POST" and p == "/v2/outbound_voice_profiles":
            pid = f"p{len(self.profiles) + 1}"; self.profiles[pid] = body
            if self.crash_on == "profile":
                return httpx.Response(500, json={"errors": [{"detail": "boom"}]})
            return httpx.Response(201, json={"data": {"id": pid, **body}})
        if m == "GET" and p == "/v2/outbound_voice_profiles":
            return httpx.Response(200, json={"data": [{"id": k, **v} for k, v in self.profiles.items()]})
        if m == "POST" and p == "/v2/credential_connections":
            cid = f"c{len(self.connections) + 1}"; self.connections[cid] = body
            return httpx.Response(201, json={"data": {"id": cid, **body}})
        if m == "GET" and p == "/v2/credential_connections":
            return httpx.Response(200, json={"data": [{"id": k, **v} for k, v in self.connections.items()]})
        if m == "POST" and p == "/v2/number_orders":
            oid = f"o{len(self.orders) + 1}"
            self.orders[oid] = {"status": self.order_status, "phone_numbers": body["phone_numbers"], "connection_id": body.get("connection_id"),
                                "customer_reference": body.get("customer_reference")}
            if self.order_status == "success":
                self.numbers["n1"] = {"phone_number": body["phone_numbers"][0]["phone_number"], "connection_id": body.get("connection_id")}
            if self.crash_on == "order":
                return httpx.Response(500, json={"errors": [{"detail": "boom"}]})
            return httpx.Response(201, json={"data": {"id": oid, **self.orders[oid]}})
        if m == "GET" and p == "/v2/number_orders":
            ref = req.url.params.get("filter[customer_reference]")
            return httpx.Response(200, json={"data": [{"id": k, **v} for k, v in self.orders.items() if v.get("customer_reference") == ref]})
        if m == "GET" and p.startswith("/v2/number_orders/"):
            oid = p.rsplit("/", 1)[1]
            return httpx.Response(200, json={"data": {"id": oid, **self.orders[oid]}})
        if m == "GET" and p == "/v2/phone_numbers":
            want = req.url.params.get("filter[phone_number]")  # no filter: every number on the account
            return httpx.Response(200, json={"data": [{"id": k, **v} for k, v in self.numbers.items() if want is None or v["phone_number"] == want]})
        if m == "PATCH" and p.startswith("/v2/phone_numbers/"):
            nid = p.rsplit("/", 1)[1]
            if nid not in self.numbers:
                return httpx.Response(404, json={"errors": [{"detail": "Resource not found"}]})
            self.patches.append((nid, body))
            self.numbers[nid].update(body)
            return httpx.Response(200, json={"data": {"id": nid, **self.numbers[nid]}})
        if m == "DELETE":
            kind, rid = p.rsplit("/", 2)[1:]
            store = {"phone_numbers": self.numbers, "credential_connections": self.connections, "outbound_voice_profiles": self.profiles}[kind]
            store.pop(rid, None)
            return httpx.Response(200, json={"data": {"id": rid}})
        if m == "POST" and p == "/v2/verified_numbers":
            self.verified[body["phone_number"]] = "pending"
            return httpx.Response(200, json={"data": {"phone_number": body["phone_number"], "verification_method": body["verification_method"]}})
        if m == "POST" and p.endswith("/actions/verify"):
            ok = body["verification_code"] == "123456"
            return httpx.Response(200 if ok else 400, json={"data": {"phone_number": p.split("/")[3]}} if ok else {"errors": [{"detail": "bad code"}]})
        return httpx.Response(404, json={"errors": [{"detail": f"unexpected {m} {p}"}]})


def _provider(carrier):
    # the same base URL production uses, so the /v2 path composition under test is the real one
    return TelnyxProvider(key="KEY", http=httpx.Client(transport=httpx.MockTransport(carrier), base_url=TELNYX_API))


def test_default_client_targets_the_v2_api():
    p = TelnyxProvider(key="KEY")
    assert str(p.http.base_url).rstrip("/") == "https://api.telnyx.com"
    req = p.http.build_request("GET", "/v2/available_phone_numbers")
    assert str(req.url) == "https://api.telnyx.com/v2/available_phone_numbers"
    assert p.http.timeout.read == 20.0


def test_injected_client_still_gets_the_request_timeout():
    seen = {}

    def handler(req):
        seen["timeout"] = req.extensions.get("timeout")
        return httpx.Response(200, json={"data": []})

    p = TelnyxProvider(key="KEY", http=httpx.Client(transport=httpx.MockTransport(handler), base_url=TELNYX_API, timeout=None))
    p.search_numbers("GB", None, 1)
    assert seen["timeout"]["read"] == 20.0


def test_cidrs_are_pinned_and_valid():
    import ipaddress
    assert SIGNALLING_CIDRS and all(ipaddress.ip_network(c) for c in SIGNALLING_CIDRS)


def test_search_and_provision_ready(tmp_path: Path):
    carrier = Carrier()
    p = _provider(carrier)
    nums = p.search_numbers("GB", "20", 5)
    assert nums[0].e164 == "+442079460000" and nums[0].monthly_cost == 1.0 and nums[0].currency == "GBP" and not nums[0].requires_regulatory
    j = Journal.open(tmp_path, "main", "telnyx")
    r = p.provision(j, nums[0], "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "ready"
    assert r.line["registrar"] == "sip:sip.telnyx.com" and r.line["dial_domain"] == "sip.telnyx.com" and r.line["user"] == "aimain1234"
    assert r.line["number"] == "+442079460000" and r.line["caller_id"] == "+442079460000" and r.line["trusted_cidrs"] == SIGNALLING_CIDRS
    assert r.line["provider"]["resources"] == {"outbound_voice_profile_id": "p1", "connection_id": "c1", "order_id": "o1", "number_id": "n1"}
    conn = carrier.connections["c1"]
    assert conn["connection_name"] == "ai-phone:main" and conn["user_name"] == "aimain1234" and conn["outbound"]["outbound_voice_profile_id"] == "p1"
    assert carrier.profiles["p1"]["whitelisted_destinations"] == ["CA", "GB", "US"], "the number's own country must be callable"


def test_pending_order_then_poll(tmp_path: Path):
    carrier = Carrier(order_status="pending")
    p = _provider(carrier)
    j = Journal.open(tmp_path, "main", "telnyx")
    r = p.provision(j, p.search_numbers("GB", None, 1)[0], "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "pending" and j.state == "pending"
    carrier.orders["o1"]["status"] = "success"
    carrier.numbers["n1"] = {"phone_number": "+442079460000", "connection_id": "c1"}
    assert p.poll(Journal.open(tmp_path, "main")).state == "ready"


def test_reconciles_by_name_after_a_crash(tmp_path: Path):
    carrier = Carrier(crash_on="profile")
    p = _provider(carrier)
    j = Journal.open(tmp_path, "main", "telnyx")
    with pytest.raises(ProviderError):
        p.provision(j, p.search_numbers("GB", None, 1)[0], "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert len(carrier.profiles) == 1   # the carrier created it, the 500 hid the id from us
    carrier.crash_on = None
    r = p.provision(Journal.open(tmp_path, "main"), p.search_numbers("GB", None, 1)[0], "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "ready" and len(carrier.profiles) == 1 and r.line["provider"]["resources"]["outbound_voice_profile_id"] == "p1"


def test_order_crash_is_reconciled_by_customer_reference(tmp_path: Path):
    carrier = Carrier(crash_on="order")
    p = _provider(carrier)
    n = p.search_numbers("GB", None, 1)[0]
    j = Journal.open(tmp_path, "main", "telnyx")
    with pytest.raises(ProviderError):
        p.provision(j, n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert len(carrier.orders) == 1 and carrier.orders["o1"]["customer_reference"] == "ai-phone:main"
    carrier.crash_on = None
    r = p.provision(Journal.open(tmp_path, "main"), n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "ready" and len(carrier.orders) == 1 and r.line["provider"]["resources"]["order_id"] == "o1"


def test_success_without_a_listed_number_stays_pending(tmp_path: Path):
    carrier = Carrier()
    p = _provider(carrier)
    n = p.search_numbers("GB", None, 1)[0]
    j = Journal.open(tmp_path, "main", "telnyx")
    r = p.provision(j, n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "ready"
    carrier.numbers.clear()  # a fresh journal whose order succeeded but whose number is not visible yet
    j2 = Journal.open(tmp_path, "later", "telnyx")
    j2.start("profile", name="x"); j2.finish("profile", outbound_voice_profile_id="p1")
    j2.start("connection", name="x", user_name="aimain1234"); j2.finish("connection", connection_id="c1")
    j2.start("order", phone_number="+442079460000", customer_reference="ai-phone:later"); j2.finish("order", order_id="o9", order_status="success")
    assert p.poll(j2).state == "pending" and j2.state == "pending"


def test_release_deletes_in_reverse(tmp_path: Path):
    carrier = Carrier()
    p = _provider(carrier)
    j = Journal.open(tmp_path, "main", "telnyx")
    p.provision(j, p.search_numbers("GB", None, 1)[0], "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    res = p.release(j)
    assert res.ok and not carrier.numbers and not carrier.connections and not carrier.profiles
    deletes = [c for c in carrier.calls if c[0] == "DELETE"]
    assert [d[1].split("/")[2] for d in deletes] == ["phone_numbers", "credential_connections", "outbound_voice_profiles"]


def test_caller_id_verification():
    p = _provider(Carrier())
    step = p.start_caller_id_verification("+447700900123")
    assert step.kind == "enter_code_in_cli" and "+447700900123" in step.instructions
    assert p.complete_caller_id_verification(step, "999999") is False
    assert p.complete_caller_id_verification(step, "123456") is True


def test_errors_never_leak_the_key_the_password_or_the_body(tmp_path: Path):
    def boom(req):
        if req.url.path == "/v2/available_phone_numbers":
            return httpx.Response(401, json={"errors": [{"detail": "bad key KEY"}]})
        return httpx.Response(500, text="<html>internal error dumping request: password=Passw0rdPassw0rdPassw0rd</html>")
    p = TelnyxProvider(key="KEY", http=httpx.Client(transport=httpx.MockTransport(boom), base_url=TELNYX_API))
    with pytest.raises(ProviderError) as e:
        p.search_numbers("GB", None, 1)
    assert "KEY" not in str(e.value)
    j = Journal.open(tmp_path, "main", "telnyx")
    with pytest.raises(ProviderError) as e2:
        p.provision(j, Number("+442079460000", 1.0, "GBP", False, "x"), "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert "Passw0rd" not in str(e2.value) and "<html>" not in str(e2.value) and "non-JSON" in str(e2.value)


def _capped(carrier: Carrier) -> TelnyxProvider:
    """Telnyx: 'You may only have 1 Outbound Voice Profile(s) at your account level' (seen on a freshly paid account)."""
    carrier.profiles["p0"] = {"name": "default", "traffic_type": "conversational"}
    inner = carrier.__call__

    def capped(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/v2/outbound_voice_profiles":
            carrier.calls.append((req.method, req.url.path))
            return httpx.Response(403, json={"errors": [{"detail": "You may only have 1 Outbound Voice Profile(s) at your account level."}]})
        return inner(req)

    return TelnyxProvider(key="KEY", http=httpx.Client(transport=httpx.MockTransport(capped), base_url=TELNYX_API))


def test_profile_cap_shares_the_accounts_profile_and_release_keeps_it(tmp_path: Path):
    carrier = Carrier()
    p = _capped(carrier)
    j = Journal.open(tmp_path, "gate", "telnyx")
    r = p.provision(j, p.search_numbers("GB", None, 1)[0], "gate", "aigate1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "ready"
    assert r.line["provider"]["resources"]["outbound_voice_profile_id"] == "p0" and "profile_shared" not in r.line["provider"]["resources"]
    assert j.resources()["profile_shared"] is True  # the journal keeps the flag `release` reads
    assert carrier.connections["c1"]["outbound"]["outbound_voice_profile_id"] == "p0"
    rel = p.release(j)
    assert rel.ok and "p0" in carrier.profiles, "the account's own profile must survive `lines remove`"
    assert "c1" not in carrier.connections and "n1" not in carrier.numbers


def test_profile_cap_without_any_profile_is_an_error(tmp_path: Path):
    carrier = Carrier()
    inner = carrier.__call__

    def capped(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/v2/outbound_voice_profiles":
            return httpx.Response(403, json={"errors": [{"detail": "You may only have 1 Outbound Voice Profile(s) at your account level."}]})
        return inner(req)

    p = TelnyxProvider(key="KEY", http=httpx.Client(transport=httpx.MockTransport(capped), base_url=TELNYX_API))
    j = Journal.open(tmp_path, "gate", "telnyx")
    with pytest.raises(ProviderError, match="no outbound voice profile to share"):
        p.provision(j, p.search_numbers("GB", None, 1)[0], "gate", "aigate1234", "Passw0rdPassw0rdPassw0rd")


def test_shared_profile_line_loads(tmp_path: Path, monkeypatch):
    """The config's provider.resources holds strings only: a journal flag copied into it made the line unloadable."""
    from ai_phone.config import load_config
    from ai_phone.lines.config_writer import add_line
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("inbound:\n  greeting: Hi\n")
    p = _capped(Carrier())
    r = p.provision(Journal.open(tmp_path / "lines", "gate", "telnyx"), p.search_numbers("GB", None, 1)[0], "gate", "aigate1234",
                    "Passw0rdPassw0rdPassw0rd")
    assert "profile_shared" not in r.line["provider"]["resources"]
    add_line(cfg, "gate", r.line)
    ln = load_config(cfg).lines["gate"]
    assert ln.provider.resources["outbound_voice_profile_id"] == "p0" and ln.number == "+442079460000"


# ---- a number the person already has ------------------------------------------------------------

def _owned(carrier: Carrier, **number) -> Carrier:
    carrier.numbers["n9"] = {"phone_number": "+15550100001", **number}
    return carrier


def test_owned_numbers_lists_the_account_numbers():
    carrier = _owned(Carrier(), connection_id="old-conn")
    p = _provider(carrier)
    assert [(n.e164, n.source_id) for n in p.owned_numbers()] == [("+15550100001", "n9")]
    assert [c[0] for c in carrier.calls] == ["GET"]


def test_attach_connects_an_owned_number_without_ordering(tmp_path: Path):
    carrier = _owned(Carrier(), connection_id="old-conn")
    p = _provider(carrier)
    j = Journal.open(tmp_path, "main", "telnyx")
    r = p.attach(j, Number("+15550100001", None, None, False, "n9"), "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert ("POST", "/v2/number_orders") not in carrier.calls and not carrier.orders
    assert carrier.numbers["n9"]["connection_id"] == "c1" and carrier.connections["c1"]["connection_name"] == "ai-phone:main"
    assert carrier.patches == [("n9", {"connection_id": "c1"})]
    assert r.state == "ready" and j.state == "ready" and r.line["number"] == "+15550100001" and r.line["user"] == "aimain1234"
    assert r.line["provider"]["resources"] == {"outbound_voice_profile_id": "p1", "connection_id": "c1", "brought_number_id": "n9"}
    res = j.resources()
    assert res["number_brought"] is True and res["brought_number_id"] == "n9" and "number_id" not in res
    assert res["previous_connection_id"] == "old-conn"
    assert carrier.profiles["p1"]["whitelisted_destinations"] == ["CA", "US"]
    assert [s["name"] for s in j.doc["steps"]] == ["claim", "profile", "connection", "route"]
    assert p.poll(j).line["number"] == "+15550100001"


def test_attach_refuses_a_number_not_on_the_account(tmp_path: Path):
    carrier = Carrier()
    p = _provider(carrier)
    with pytest.raises(ProviderError, match="not on this Telnyx account"):
        p.attach(Journal.open(tmp_path, "main", "telnyx"), Number("+15550100001", None, None, False, "n9"), "main", "aimain1234", "pw")
    assert carrier.profiles == {} and carrier.connections == {}


def test_attach_resumes_after_a_lost_route_response(tmp_path: Path):
    carrier = _owned(Carrier(), connection_id="old-conn")
    lost = {"armed": True}

    def handler(req):
        r = carrier(req)
        if lost["armed"] and req.method == "PATCH":
            lost["armed"] = False
            raise httpx.ConnectError("lost")
        return r

    p = TelnyxProvider(key="KEY", http=httpx.Client(transport=httpx.MockTransport(handler), base_url=TELNYX_API))
    n = Number("+15550100001", None, None, False, "n9")
    with pytest.raises(ProviderError):
        p.attach(Journal.open(tmp_path, "main", "telnyx"), n, "main", "aimain1234", "pw")
    j = Journal.open(tmp_path, "main")
    r = p.attach(j, n, "main", "aimain1234", "pw")
    assert r.state == "ready" and len(carrier.profiles) == 1 and len(carrier.connections) == 1
    assert j.resources()["previous_connection_id"] == "old-conn"  # the claim was not re-read after the move


def test_release_of_a_brought_number_reconnects_it_and_keeps_it(tmp_path: Path):
    carrier = _owned(Carrier(), connection_id="old-conn")
    p = _provider(carrier)
    j = Journal.open(tmp_path, "main", "telnyx")
    p.attach(j, Number("+15550100001", None, None, False, "n9"), "main", "aimain1234", "pw")
    before = len(carrier.calls)
    assert p.release(j).ok
    assert ("DELETE", "/v2/phone_numbers/n9") not in carrier.calls and "n9" in carrier.numbers
    assert carrier.numbers["n9"]["connection_id"] == "old-conn"
    assert not carrier.connections and not carrier.profiles
    order = [c for c in carrier.calls[before:] if c[0] in ("PATCH", "DELETE")]
    assert order == [("PATCH", "/v2/phone_numbers/n9"), ("DELETE", "/v2/credential_connections/c1"),
                     ("DELETE", "/v2/outbound_voice_profiles/p1")], "the number leaves the connection before it is deleted"


def test_release_of_an_old_format_brought_number_keeps_it(tmp_path: Path):
    """What e4a8c67 wrote: the brought number under `number_id`, flagged `number_brought`."""
    carrier = _owned(Carrier(), connection_id="old-conn")
    p = _provider(carrier)
    j = Journal.open(tmp_path, "main", "telnyx")
    p.attach(j, Number("+15550100001", None, None, False, "n9"), "main", "aimain1234", "pw")
    for s in j.doc["steps"]:
        if "brought_number_id" in s["result"]:
            s["result"]["number_id"] = s["result"].pop("brought_number_id")
    j._save()
    assert j.resources()["number_id"] == "n9" and j.resources()["number_brought"] is True
    assert p.release(j).ok
    assert ("DELETE", "/v2/phone_numbers/n9") not in carrier.calls and "n9" in carrier.numbers
    assert carrier.numbers["n9"]["connection_id"] == "old-conn"
    assert not carrier.connections and not carrier.profiles


def test_release_of_a_brought_number_with_no_previous_connection_sends_null(tmp_path: Path):
    carrier = _owned(Carrier())
    p = _provider(carrier)
    j = Journal.open(tmp_path, "main", "telnyx")
    p.attach(j, Number("+15550100001", None, None, False, "n9"), "main", "aimain1234", "pw")
    assert j.resources()["previous_connection_id"] == ""
    assert p.release(j).ok
    assert carrier.patches[-1] == ("n9", {"connection_id": None}) and "n9" in carrier.numbers


def test_release_of_a_brought_number_that_cannot_be_moved_still_deletes_the_rest(tmp_path: Path):
    carrier = _owned(Carrier(), connection_id="old-conn")
    p = _provider(carrier)
    j = Journal.open(tmp_path, "main", "telnyx")
    p.attach(j, Number("+15550100001", None, None, False, "n9"), "main", "aimain1234", "pw")
    del carrier.numbers["n9"]  # the move back is refused (404)
    res = p.release(j)
    assert not res.ok and "stays on your account" in res.leftover["brought_number_id"] and j.state == "rollback_failed"
    assert not [c for c in carrier.calls if c == ("DELETE", "/v2/phone_numbers/n9")]
    assert not carrier.connections and not carrier.profiles


def test_owned_numbers_carry_their_country():
    from ai_phone.providers.telnyx import _country_of
    carrier = _owned(Carrier())
    carrier.numbers["n8"] = {"phone_number": "+442079460001"}
    carrier.numbers["n7"] = {"phone_number": "+15550100002", "country_iso_alpha2": "CA"}  # the record's own country wins
    assert [(n.e164, n.country) for n in _provider(carrier).owned_numbers()] == [
        ("+15550100001", "US"), ("+442079460001", "GB"), ("+15550100002", "CA")]
    assert _country_of("+447700900123") == "GB" and _country_of("+9990000000") == ""  # +999 is not assigned to anyone


def test_attaching_a_uk_number_whitelists_gb(tmp_path: Path):
    """A brought number must be able to call its own country, as a bought one can."""
    carrier = Carrier()
    carrier.numbers["n9"] = {"phone_number": "+442079460001", "connection_id": "old-conn"}
    p = _provider(carrier)
    r = p.attach(Journal.open(tmp_path, "main", "telnyx"), Number("+442079460001", None, None, False, "n9"), "main", "aimain1234", "pw")
    assert r.state == "ready" and carrier.profiles["p1"]["whitelisted_destinations"] == ["CA", "GB", "US"]


def test_resuming_a_uk_purchase_still_whitelists_gb(tmp_path: Path):
    """`lines resume` passes a number without a country: the profile still allows the number's own country."""
    carrier = Carrier()
    p = _provider(carrier)
    r = p.provision(Journal.open(tmp_path, "main", "telnyx"), Number("+442079460000", None, None, False, "+442079460000"),
                    "main", "aimain1234", "pw")
    assert r.state == "ready" and carrier.profiles["p1"]["whitelisted_destinations"] == ["CA", "GB", "US"]


def test_a_buy_then_a_bring_in_one_journal_releases_the_bought_number(tmp_path: Path):
    """A journal shared by a purchase whose line was never written and a later attach: the bought number is still
    deleted, the brought one moved back and kept."""
    carrier = _owned(Carrier(), connection_id="old-conn")
    p = _provider(carrier)
    j = Journal.open(tmp_path, "main", "telnyx")
    assert p.provision(j, p.search_numbers("GB", None, 1)[0], "main", "aimain1234", "pw").state == "ready"
    assert p.attach(j, Number("+15550100001", None, None, False, "n9"), "main", "aimain1234", "pw").state == "ready"
    res = j.resources()
    assert res["number_id"] == "n1" and res["brought_number_id"] == "n9"
    assert p.release(j).ok
    assert list(carrier.numbers) == ["n9"] and carrier.numbers["n9"]["connection_id"] == "old-conn"
    assert not carrier.connections and not carrier.profiles


def test_a_bring_then_a_buy_in_one_journal_restores_the_brought_number(tmp_path: Path):
    carrier = _owned(Carrier(), connection_id="old-conn")
    refused = {"done": False}

    def handler(req):
        if not refused["done"] and req.method == "PATCH":
            refused["done"] = True
            return httpx.Response(500, json={"errors": [{"detail": "try again"}]})
        return carrier(req)

    p = TelnyxProvider(key="KEY", http=httpx.Client(transport=httpx.MockTransport(handler), base_url=TELNYX_API))
    j = Journal.open(tmp_path, "main", "telnyx")
    with pytest.raises(ProviderError):
        p.attach(j, Number("+15550100001", None, None, False, "n9"), "main", "aimain1234", "pw")
    r = p.provision(j, p.search_numbers("GB", None, 1)[0], "main", "aimain1234", "pw")
    assert r.state == "ready" and r.line["number"] == "+442079460000" and r.line["provider"]["resources"]["number_id"] == "n1"
    assert p.release(j).ok
    assert list(carrier.numbers) == ["n9"], "the bought number is released"
    assert carrier.numbers["n9"]["connection_id"] == "old-conn" and not carrier.connections and not carrier.profiles
