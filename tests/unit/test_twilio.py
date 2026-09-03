import json
import urllib.parse
from pathlib import Path

import httpx
import pytest

from ai_phone.lines.journal import Journal
from ai_phone.providers.base import ProviderError
from ai_phone.providers.twilio import SIGNALLING_CIDRS, TwilioProvider

SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class Carrier:
    def __init__(self):
        self.credlists, self.creds, self.domains, self.mappings, self.numbers, self.trunks, self.trunk_cls, self.callerids = {}, {}, {}, {}, {}, {}, {}, {}
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.host, req.url.path))
        assert req.headers["authorization"].startswith("Basic ")
        form = dict(urllib.parse.parse_qsl(req.content.decode())) if req.content else {}
        p, m, host = req.url.path, req.method, req.url.host
        if host == "pricing.twilio.com":
            return httpx.Response(200, json={"phone_number_prices": [{"number_type": "local", "current_price": "1.15", "base_price": "1.15"}], "price_unit": "USD"})
        if m == "GET" and p.endswith("/AvailablePhoneNumbers/GB/Local.json"):
            return httpx.Response(200, json={"available_phone_numbers": [{"phone_number": "+442079460000"}]})
        if m == "POST" and p.endswith("/SIP/CredentialLists.json"):
            sid = f"CL{len(self.credlists) + 1}"; self.credlists[sid] = form; return httpx.Response(201, json={"sid": sid, **form})
        if m == "GET" and p.endswith("/SIP/CredentialLists.json"):
            return httpx.Response(200, json={"credential_lists": [{"sid": k, "friendly_name": v["FriendlyName"]} for k, v in self.credlists.items()]})
        if m == "POST" and "/Credentials.json" in p:
            sid = f"CR{len(self.creds) + 1}"; self.creds[sid] = form; return httpx.Response(201, json={"sid": sid})
        if m == "GET" and "/Credentials.json" in p:
            return httpx.Response(200, json={"credentials": [{"sid": k, "username": v["Username"]} for k, v in self.creds.items()]})
        if m == "POST" and p.endswith("/SIP/Domains.json"):
            sid = f"SD{len(self.domains) + 1}"; self.domains[sid] = form; return httpx.Response(201, json={"sid": sid, **form})
        if m == "GET" and p.endswith("/SIP/Domains.json"):
            return httpx.Response(200, json={"domains": [{"sid": k, "domain_name": v["DomainName"], "friendly_name": v.get("FriendlyName")} for k, v in self.domains.items()]})
        if m == "POST" and "/Auth/Registrations/CredentialListMappings.json" in p:
            self.mappings[p.split("/")[6]] = form["CredentialListSid"]; return httpx.Response(201, json={"sid": "ML1"})
        if m == "GET" and "/Auth/Registrations/CredentialListMappings.json" in p:
            dsid = p.split("/")[6]
            return httpx.Response(200, json={"contents": [{"sid": self.mappings[dsid]}] if dsid in self.mappings else []})
        if m == "POST" and p.endswith("/IncomingPhoneNumbers.json"):
            sid = f"PN{len(self.numbers) + 1}"; self.numbers[sid] = form; return httpx.Response(201, json={"sid": sid, **form})
        if m == "GET" and p.endswith("/IncomingPhoneNumbers.json"):
            want = req.url.params.get("PhoneNumber")
            return httpx.Response(200, json={"incoming_phone_numbers": [{"sid": k, "phone_number": v["PhoneNumber"]} for k, v in self.numbers.items() if v["PhoneNumber"] == want]})
        if host == "trunking.twilio.com" and m == "POST" and p == "/v1/Trunks":
            sid = f"TK{len(self.trunks) + 1}"; self.trunks[sid] = form; return httpx.Response(201, json={"sid": sid, **form})
        if host == "trunking.twilio.com" and m == "GET" and p == "/v1/Trunks":
            return httpx.Response(200, json={"trunks": [{"sid": k, "friendly_name": v["FriendlyName"], "domain_name": v["DomainName"]} for k, v in self.trunks.items()]})
        if host == "trunking.twilio.com" and m == "POST" and p.endswith("/CredentialLists"):
            self.trunk_cls[p.split("/")[3]] = form["CredentialListSid"]; return httpx.Response(201, json={"sid": form["CredentialListSid"]})
        if host == "trunking.twilio.com" and m == "GET" and p.endswith("/CredentialLists"):
            tsid = p.split("/")[3]
            return httpx.Response(200, json={"credential_lists": [{"sid": self.trunk_cls[tsid]}] if tsid in self.trunk_cls else []})
        if m == "DELETE":
            for store in (self.numbers, self.domains, self.credlists, self.trunks):
                store.pop(p.rstrip(".json").rsplit("/", 1)[1], None)
            return httpx.Response(204)
        if m == "POST" and p.endswith("/OutgoingCallerIds.json"):
            self.callerids[form["PhoneNumber"]] = "pending"
            return httpx.Response(201, json={"validation_code": "654321", "phone_number": form["PhoneNumber"]})
        if m == "GET" and p.endswith("/OutgoingCallerIds.json"):
            n = req.url.params.get("PhoneNumber")
            return httpx.Response(200, json={"outgoing_caller_ids": [{"phone_number": n}] if self.callerids.get(n) == "verified" else []})
        return httpx.Response(404, json={"message": f"unexpected {m} {host}{p}"})


def _provider(carrier):
    return TwilioProvider(key=SID, secret="tok", http=httpx.Client(transport=httpx.MockTransport(carrier)))


def test_cidrs_pinned():
    import ipaddress
    assert SIGNALLING_CIDRS and all(ipaddress.ip_network(c) for c in SIGNALLING_CIDRS)


def test_search_uses_pricing_api():
    n = _provider(Carrier()).search_numbers("GB", "20", 5)[0]
    assert n.e164 == "+442079460000" and n.monthly_cost == 1.15 and n.currency == "USD"


def test_provision_creates_domain_number_trunk(tmp_path: Path):
    c = Carrier()
    p = _provider(c)
    j = Journal.open(tmp_path, "main", "twilio")
    r = p.provision(j, p.search_numbers("GB", None, 1)[0], "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "ready"
    dom = next(iter(c.domains.values()))
    assert dom["DomainName"].startswith("ai-phone-main-") and dom["DomainName"].endswith(".sip.twilio.com") and dom["SipRegistration"] == "true"
    assert c.mappings["SD1"] == "CL1" and c.creds["CR1"]["Username"] == "aimain1234"
    num = next(iter(c.numbers.values()))
    assert num["PhoneNumber"] == "+442079460000" and "twimlets.com/echo" in num["VoiceUrl"] and "aimain1234%40ai-phone-main-" in num["VoiceUrl"]
    trunk = next(iter(c.trunks.values()))
    assert trunk["DomainName"].endswith(".pstn.twilio.com") and c.trunk_cls["TK1"] == "CL1"
    assert r.line["registrar"] == f"sip:{dom['DomainName']}" and r.line["outbound_proxy"] == f"sip:{trunk['DomainName']}"
    assert r.line["dial_domain"] == trunk["DomainName"] and r.line["register_via_proxy"] is False and r.line["user"] == "aimain1234"
    assert r.line["provider"]["resources"]["domain_sid"] == "SD1"
    assert [s["name"] for s in j.doc["steps"]] == ["credential_list", "credential", "domain", "domain_mapping", "number", "trunk", "trunk_credential_list"]


def test_planned_domain_name_is_journaled_before_creation_and_reused(tmp_path: Path):
    c = Carrier()
    crash = {"armed": True}

    def handler(req):  # the carrier creates the domain, then the response is lost before the journal's `done`
        r = c(req)
        if crash["armed"] and req.method == "POST" and req.url.path.endswith("/SIP/Domains.json"):
            raise httpx.ConnectError("lost")
        return r

    p = TwilioProvider(key=SID, secret="tok", http=httpx.Client(transport=httpx.MockTransport(handler)))
    j = Journal.open(tmp_path, "main", "twilio")
    n = p.search_numbers("GB", None, 1)[0]
    with pytest.raises(ProviderError):
        p.provision(j, n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    planned = next(s["planned"]["domain_name"] for s in j.doc["steps"] if s["name"] == "domain")
    assert "SD1" not in c.mappings  # the crash happened before the registration mapping
    crash["armed"] = False
    r = p.provision(Journal.open(tmp_path, "main"), n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert len(c.domains) == 1 and next(iter(c.domains.values()))["DomainName"] == planned and r.state == "ready"
    assert c.mappings["SD1"] == "CL1" and len(c.creds) == 1 and c.trunk_cls["TK1"] == "CL1"  # the steps after the crash ran once each


MUTATING = 7  # credential list, credential, domain, mapping, number, trunk, trunk credential list


@pytest.mark.parametrize("crash_at", list(range(1, MUTATING + 1)))
def test_a_lost_response_after_any_step_is_reconciled_without_duplicates(tmp_path: Path, crash_at: int):
    c = Carrier()
    posts = {"n": 0, "armed": True}

    def handler(req):
        r = c(req)  # the carrier always processes the request; we lose the response on the crash_at-th mutation
        if req.method == "POST" and posts["armed"]:
            posts["n"] += 1
            if posts["n"] == crash_at:
                raise httpx.ConnectError("lost")
        return r

    p = TwilioProvider(key=SID, secret="tok", http=httpx.Client(transport=httpx.MockTransport(handler)))
    n = p.search_numbers("GB", None, 1)[0]
    with pytest.raises(ProviderError):
        p.provision(Journal.open(tmp_path, "main", "twilio"), n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    posts["armed"] = False
    r = p.provision(Journal.open(tmp_path, "main"), n, "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert r.state == "ready"
    assert (len(c.credlists), len(c.creds), len(c.domains), len(c.mappings), len(c.numbers), len(c.trunks), len(c.trunk_cls)) == (1, 1, 1, 1, 1, 1, 1)


def test_errors_never_leak_the_sid_or_token():
    def boom(req):
        return httpx.Response(401, json={"message": f"bad auth for {SID} with tok"})
    p = TwilioProvider(key=SID, secret="tok", http=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(ProviderError) as e:
        p.search_numbers("GB", None, 1)
    assert SID not in str(e.value) and "tok" not in str(e.value).replace("token", "")


def test_injected_client_still_gets_the_request_timeout():
    seen = {}

    def handler(req):
        seen["timeout"] = req.extensions.get("timeout")
        return httpx.Response(200, json={"available_phone_numbers": [], "phone_number_prices": [], "price_unit": "USD"})

    p = TwilioProvider(key=SID, secret="tok", http=httpx.Client(transport=httpx.MockTransport(handler), timeout=None))
    p.search_numbers("GB", None, 1)
    assert seen["timeout"]["read"] == 20.0


def test_release_and_caller_id(tmp_path: Path):
    c = Carrier()
    p = _provider(c)
    j = Journal.open(tmp_path, "main", "twilio")
    p.provision(j, p.search_numbers("GB", None, 1)[0], "main", "aimain1234", "Passw0rdPassw0rdPassw0rd")
    assert p.release(j).ok and not c.numbers and not c.domains and not c.credlists and not c.trunks
    step = p.start_caller_id_verification("+447700900123")
    assert step.kind == "enter_code_on_phone" and "654321" in step.instructions
    assert p.complete_caller_id_verification(step, None) is False
    c.callerids["+447700900123"] = "verified"
    assert p.complete_caller_id_verification(step, None) is True
