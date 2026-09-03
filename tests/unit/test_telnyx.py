import json
from pathlib import Path

import httpx
import pytest

from ai_phone.lines.journal import Journal
from ai_phone.providers.base import ProviderError
from ai_phone.providers.telnyx import SIGNALLING_CIDRS, TelnyxProvider


class Carrier:
    """Just enough of api.telnyx.com/v2 to provision, poll, release and verify."""

    def __init__(self, order_status="success", crash_on=None):
        self.profiles, self.connections, self.orders, self.numbers, self.verified = {}, {}, {}, {}, {}
        self.order_status, self.crash_on = order_status, crash_on
        self.calls = []

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
            oid = f"o{len(self.orders) + 1}"; self.orders[oid] = {"status": self.order_status, "phone_numbers": body["phone_numbers"], "connection_id": body.get("connection_id")}
            if self.order_status == "success":
                self.numbers["n1"] = {"phone_number": body["phone_numbers"][0]["phone_number"], "connection_id": body.get("connection_id")}
            return httpx.Response(201, json={"data": {"id": oid, **self.orders[oid]}})
        if m == "GET" and p.startswith("/v2/number_orders/"):
            oid = p.rsplit("/", 1)[1]
            return httpx.Response(200, json={"data": {"id": oid, **self.orders[oid]}})
        if m == "GET" and p == "/v2/phone_numbers":
            return httpx.Response(200, json={"data": [{"id": k, **v} for k, v in self.numbers.items() if v["phone_number"] == req.url.params.get("filter[phone_number]")]})
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
    return TelnyxProvider(key="KEY", http=httpx.Client(transport=httpx.MockTransport(carrier), base_url="https://api.telnyx.com"))


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


def test_errors_never_leak_the_key(tmp_path: Path):
    def boom(req):
        return httpx.Response(401, json={"errors": [{"detail": "bad key KEY"}]})
    p = TelnyxProvider(key="KEY", http=httpx.Client(transport=httpx.MockTransport(boom), base_url="https://api.telnyx.com"))
    with pytest.raises(ProviderError) as e:
        p.search_numbers("GB", None, 1)
    assert "KEY" not in str(e.value)
