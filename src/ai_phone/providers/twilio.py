"""Twilio: Programmable Voice SIP Domain with registration for inbound, Elastic SIP Trunking termination for outbound.
Inbound TwiML is hosted on twimlets echo (Twilio-hosted, no server of ours). REGISTER must not go through the trunk proxy."""
from __future__ import annotations

import secrets
import urllib.parse

import httpx

from ai_phone.lines.journal import Journal
from ai_phone.providers.base import Number, ProviderError, ProvisionResult, ReleaseResult, VerificationStep

API = "https://api.twilio.com/2010-04-01/Accounts"
TRUNKING = "https://trunking.twilio.com/v1"
PRICING = "https://pricing.twilio.com/v1"
# Twilio SIP signalling ranges (every edge, so a line works whichever region Twilio routes through), from
# https://www.twilio.com/docs/sip-trunking/ip-addresses as published on 2026-09-03; the Task 0b spike note re-verifies.
SIGNALLING_CIDRS: list[str] = [
    "54.172.60.0/30", "54.244.51.0/30",           # North America (Virginia, Oregon)
    "54.171.127.192/30", "35.156.191.128/30",     # Europe (Ireland, Frankfurt)
    "54.65.63.192/30", "54.169.127.128/30", "54.252.254.64/30",   # Asia-Pacific (Tokyo, Singapore, Sydney)
    "177.71.206.192/30",                          # South America (São Paulo)
]


def _twiml_url(user: str, domain: str) -> str:
    twiml = f"<Response><Dial><Sip>sip:{user}@{domain}</Sip></Dial></Response>"
    return "https://twimlets.com/echo?Twiml=" + urllib.parse.quote(twiml, safe="")


class TwilioProvider:
    name = "twilio"
    signalling_cidrs = SIGNALLING_CIDRS

    def __init__(self, key: str, secret: str | None = None, http: httpx.Client | None = None):
        if not secret:
            raise ProviderError("twilio needs --secret-env (the Auth Token) as well as --key-env (the Account SID)")
        self.sid, self._token = key, secret
        self.http = http or httpx.Client(timeout=20.0)
        self.http.auth = (key, secret)

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***").replace(self.sid, "AC***")

    def _call(self, method: str, url: str, **kw) -> dict:
        shown = self._redact(url.split("?")[0])
        try:
            r = self.http.request(method, url, timeout=20.0, **kw)  # per request, so an injected client cannot bypass it
        except httpx.HTTPError as e:
            raise ProviderError(f"twilio {method} {shown}: {type(e).__name__}", retryable=True) from None
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = r.text[:200]
            raise ProviderError(f"twilio {method} {shown} -> {r.status_code}: {self._redact(msg)}", retryable=r.status_code >= 500)
        return r.json() if r.content else {}

    def _acct(self, path: str) -> str:
        return f"{API}/{self.sid}{path}"

    # ---- numbers ------------------------------------------------------------------------------
    def search_numbers(self, country: str, area: str | None, limit: int) -> list[Number]:
        params = {"VoiceEnabled": "true", "PageSize": str(limit)}
        if area:
            params["AreaCode"] = area
        found = self._call("GET", self._acct(f"/AvailablePhoneNumbers/{country}/Local.json"), params=params).get("available_phone_numbers", [])
        price = currency = None
        try:
            pr = self._call("GET", f"{PRICING}/PhoneNumbers/Countries/{country}")
            local = next((p for p in pr.get("phone_number_prices", []) if p.get("number_type") == "local"), None)
            price, currency = (float(local["current_price"]) if local else None), pr.get("price_unit")
        except ProviderError:
            pass
        return [Number(n["phone_number"], price, currency, False, n["phone_number"]) for n in found]

    # ---- provisioning -------------------------------------------------------------------------
    def provision(self, journal: Journal, number: Number, line_id: str, sip_user: str, sip_password: str) -> ProvisionResult:
        """One journal step per carrier request, so a crash between any request and its `done` is reconciled by name."""
        name = f"ai-phone:{line_id}"
        st, planned = journal.start("credential_list", name=name)
        if st != "done":
            rec = (self._find_credlist(name) if st == "started" else None) or self._call("POST", self._acct("/SIP/CredentialLists.json"), data={"FriendlyName": name})
            journal.finish("credential_list", credential_list_sid=rec["sid"])
        cl_sid = journal.resources()["credential_list_sid"]

        st, planned = journal.start("credential", user=sip_user)
        if st != "done":
            existing = self._find_credential(cl_sid, sip_user) if st == "started" else None
            rec = existing or self._call("POST", self._acct(f"/SIP/CredentialLists/{cl_sid}/Credentials.json"), data={"Username": sip_user, "Password": sip_password})
            journal.finish("credential", credential_sid=rec["sid"], user=sip_user)

        st, planned = journal.start("domain", domain_name=f"ai-phone-{line_id}-{secrets.token_hex(3)}.sip.twilio.com", name=name)
        domain_name = planned["domain_name"]
        if st != "done":
            rec = self._find_domain(domain_name) if st == "started" else None
            for attempt in range(2):
                if rec is not None:
                    break
                try:
                    rec = self._call("POST", self._acct("/SIP/Domains.json"),
                                     data={"DomainName": domain_name, "FriendlyName": name, "SipRegistration": "true", "VoiceMethod": "POST"})
                except ProviderError as err:
                    if attempt == 1 or "already" not in str(err).lower():
                        raise
                    domain_name = f"ai-phone-{line_id}-{secrets.token_hex(3)}.sip.twilio.com"  # someone else has that name: one fresh suffix
                    journal.doc["steps"][-1]["planned"]["domain_name"] = domain_name  # the journal always names what will be created
                    journal._save()
            journal.finish("domain", domain_sid=rec["sid"], domain_name=domain_name)
        domain_sid = journal.resources()["domain_sid"]

        st, planned = journal.start("domain_mapping", domain_sid=domain_sid, credential_list_sid=cl_sid)
        if st != "done":
            if not (st == "started" and self._domain_has_mapping(domain_sid, cl_sid)):
                self._call("POST", self._acct(f"/SIP/Domains/{domain_sid}/Auth/Registrations/CredentialListMappings.json"), data={"CredentialListSid": cl_sid})
            journal.finish("domain_mapping", mapped=True)

        st, planned = journal.start("number", phone_number=number.e164)
        if st != "done":
            rec = (self._find_number(number.e164) if st == "started" else None) or self._call(
                "POST", self._acct("/IncomingPhoneNumbers.json"),
                data={"PhoneNumber": number.e164, "FriendlyName": name, "VoiceUrl": _twiml_url(sip_user, domain_name), "VoiceMethod": "GET"})
            journal.finish("number", number_sid=rec["sid"])

        st, planned = journal.start("trunk", trunk_domain=f"ai-phone-{line_id}-{secrets.token_hex(3)}.pstn.twilio.com", name=name)
        trunk_domain = planned["trunk_domain"]
        if st != "done":
            rec = (self._find_trunk(name) if st == "started" else None) or self._call("POST", f"{TRUNKING}/Trunks", data={"FriendlyName": name, "DomainName": trunk_domain})
            journal.finish("trunk", trunk_sid=rec["sid"], trunk_domain=rec.get("domain_name", trunk_domain))
        trunk_sid = journal.resources()["trunk_sid"]

        st, planned = journal.start("trunk_credential_list", trunk_sid=trunk_sid, credential_list_sid=cl_sid)
        if st != "done":
            if not (st == "started" and self._trunk_has_credlist(trunk_sid, cl_sid)):
                self._call("POST", f"{TRUNKING}/Trunks/{trunk_sid}/CredentialLists", data={"CredentialListSid": cl_sid})
            journal.finish("trunk_credential_list", attached=True)
        journal.set_state("ready")
        return self._result(journal, number.e164)

    def poll(self, journal: Journal) -> ProvisionResult:
        e164 = next(s["planned"]["phone_number"] for s in journal.doc["steps"] if s["name"] == "number")
        journal.set_state("ready")
        return self._result(journal, e164)

    def _result(self, journal: Journal, e164: str) -> ProvisionResult:
        r = journal.resources()
        line = {"kind": "trunk", "number": e164, "caller_id": e164, "registrar": f"sip:{r['domain_name']}",
                "outbound_proxy": f"sip:{r['trunk_domain']}", "dial_domain": r["trunk_domain"], "register_via_proxy": False,
                "transport": "udp", "user": r["user"], "trusted_cidrs": list(SIGNALLING_CIDRS),
                "provider": {"name": self.name, "state": "ready", "resources": {k: r[k] for k in ("credential_list_sid", "domain_sid", "number_sid", "trunk_sid")}}}
        return ProvisionResult("ready", f"{e164} is provisioned", line)

    def _find_credlist(self, name):
        return next((c for c in self._call("GET", self._acct("/SIP/CredentialLists.json")).get("credential_lists", []) if c.get("friendly_name") == name), None)

    def _find_credential(self, cl_sid, user):
        return next((c for c in self._call("GET", self._acct(f"/SIP/CredentialLists/{cl_sid}/Credentials.json")).get("credentials", []) if c.get("username") == user), None)

    def _domain_has_mapping(self, domain_sid, cl_sid):
        return any(m.get("sid") == cl_sid or m.get("credential_list_sid") == cl_sid
                   for m in self._call("GET", self._acct(f"/SIP/Domains/{domain_sid}/Auth/Registrations/CredentialListMappings.json")).get("contents", []))

    def _trunk_has_credlist(self, trunk_sid, cl_sid):
        return any(c.get("sid") == cl_sid for c in self._call("GET", f"{TRUNKING}/Trunks/{trunk_sid}/CredentialLists").get("credential_lists", []))

    def _find_domain(self, domain_name):
        return next((d for d in self._call("GET", self._acct("/SIP/Domains.json")).get("domains", []) if d.get("domain_name") == domain_name), None)

    def _find_number(self, e164):
        return next(iter(self._call("GET", self._acct("/IncomingPhoneNumbers.json"), params={"PhoneNumber": e164}).get("incoming_phone_numbers", [])), None)

    def _find_trunk(self, name):
        return next((t for t in self._call("GET", f"{TRUNKING}/Trunks").get("trunks", []) if t.get("friendly_name") == name), None)

    def release(self, journal: Journal) -> ReleaseResult:
        r = journal.resources()
        leftover = {}
        for key, url in (("trunk_sid", lambda s: f"{TRUNKING}/Trunks/{s}"), ("number_sid", lambda s: self._acct(f"/IncomingPhoneNumbers/{s}.json")),
                         ("domain_sid", lambda s: self._acct(f"/SIP/Domains/{s}.json")), ("credential_list_sid", lambda s: self._acct(f"/SIP/CredentialLists/{s}.json"))):
            sid = r.get(key)
            if not sid:
                continue
            try:
                self._call("DELETE", url(sid))
            except ProviderError as e:
                leftover[key] = f"{sid}: {e}"
        if leftover:
            journal.set_state("rollback_failed")
            return ReleaseResult(False, leftover)
        journal.set_state("pending")
        return ReleaseResult(True, {})

    # ---- caller id ----------------------------------------------------------------------------
    def start_caller_id_verification(self, e164: str) -> VerificationStep:
        rec = self._call("POST", self._acct("/OutgoingCallerIds.json"), data={"PhoneNumber": e164, "FriendlyName": "ai-phone"})
        return VerificationStep("enter_code_on_phone", f"Twilio is calling {e164}. Answer and key in this code: {rec['validation_code']}", {"e164": e164})

    def complete_caller_id_verification(self, step: VerificationStep, code: str | None) -> bool:
        ids = self._call("GET", self._acct("/OutgoingCallerIds.json"), params={"PhoneNumber": step.handle["e164"]}).get("outgoing_caller_ids", [])
        return any(i.get("phone_number") == step.handle["e164"] for i in ids)
