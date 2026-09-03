"""Telnyx: credential SIP connection with registration, a number ordered onto it, an outbound voice profile.
Every step is journaled and reconciled by name (`ai-phone:<line-id>`) so re-runs never duplicate resources."""
from __future__ import annotations

import httpx

from ai_phone.lines.journal import Journal
from ai_phone.providers.base import Number, ProviderError, ProvisionResult, ReleaseResult, VerificationStep

TELNYX_API = "https://api.telnyx.com"  # paths below carry the /v2 prefix themselves
REGISTRAR = "sip:sip.telnyx.com"
DIAL_DOMAIN = "sip.telnyx.com"
# Telnyx SIP signalling addresses (the regional SIP proxies that send us INVITEs), from https://sip.telnyx.com/
# as published on 2026-09-03; the Task 0a spike note re-verifies them. Media comes from other ranges and is not policed.
SIGNALLING_CIDRS: list[str] = [
    "192.76.120.10/32", "64.16.250.10/32",        # sip.telnyx.com (US)
    "185.246.41.140/32", "185.246.41.141/32",     # sip.telnyx.eu
    "192.76.120.31/32", "64.16.250.13/32",        # sip.telnyx.ca
    "103.115.244.145/32", "103.115.244.146/32",   # sip.telnyx.com.au
    "185.246.42.128/32", "185.246.42.129/32",     # sip.telnyx.me
    "103.115.244.158/32", "103.115.244.159/32",   # sip.telnyx.asia
]


def _detail(r: httpx.Response) -> str:
    try:
        return "; ".join(e.get("detail", "") for e in r.json().get("errors", [])) or r.text[:200]
    except ValueError:
        return r.text[:200]


class TelnyxProvider:
    name = "telnyx"
    signalling_cidrs = SIGNALLING_CIDRS

    def __init__(self, key: str, secret: str | None = None, http: httpx.Client | None = None):
        self._key = key
        self.http = http or httpx.Client(base_url=TELNYX_API, timeout=20.0)
        self.http.headers["Authorization"] = f"Bearer {key}"

    # ---- http ---------------------------------------------------------------------------------
    def _call(self, method: str, path: str, **kw) -> dict | list:
        try:
            r = self.http.request(method, f"/v2{path}", timeout=20.0, **kw)  # per request, so an injected client cannot bypass it
        except httpx.HTTPError as e:
            raise ProviderError(f"telnyx {method} {path}: {type(e).__name__}", retryable=True) from None
        if r.status_code >= 400:
            raise ProviderError(f"telnyx {method} {path} -> {r.status_code}: {_detail(r).replace(self._key, '***')}",
                                retryable=r.status_code >= 500)
        return r.json().get("data", {}) if r.content else {}

    def _find(self, path: str, key: str, value: str) -> dict | None:
        for item in self._call("GET", path, params={f"filter[{key}][contains]": value}) or []:
            if item.get(key) == value:
                return item
        return None

    # ---- numbers ------------------------------------------------------------------------------
    def search_numbers(self, country: str, area: str | None, limit: int) -> list[Number]:
        params = {"filter[country_code]": country, "filter[features][]": "voice", "filter[limit]": str(limit)}
        if area:
            params["filter[national_destination_code]"] = area
        out = []
        for n in self._call("GET", "/available_phone_numbers", params=params) or []:
            ci = n.get("cost_information") or {}
            cost = ci.get("monthly_cost")
            out.append(Number(n["phone_number"], float(cost) if cost is not None else None, ci.get("currency"),
                              bool(n.get("requirements")), n["phone_number"]))
        return out

    def _exact_number(self, e164: str) -> None:
        """Telnyx only orders numbers returned by a recent search: search this exact number right before ordering."""
        if not any(n["phone_number"] == e164 for n in self._call("GET", "/available_phone_numbers", params={"filter[phone_number][starts_with]": e164, "filter[limit]": "5"}) or []):
            raise ProviderError(f"{e164} is no longer available")

    # ---- provisioning -------------------------------------------------------------------------
    def provision(self, journal: Journal, number: Number, line_id: str, sip_user: str, sip_password: str) -> ProvisionResult:
        name = f"ai-phone:{line_id}"
        st, data = journal.start("profile", name=name)
        if st != "done":
            existing = self._find("/outbound_voice_profiles", "name", name) if st == "started" else None
            rec = existing or self._call("POST", "/outbound_voice_profiles", json={"name": name, "traffic_type": "conversational"})
            journal.finish("profile", outbound_voice_profile_id=rec["id"])
        profile_id = journal.resources()["outbound_voice_profile_id"]

        st, data = journal.start("connection", name=name, user_name=sip_user)
        if st != "done":
            existing = self._find("/credential_connections", "connection_name", name) if st == "started" else None
            body = {"connection_name": name, "user_name": sip_user, "password": sip_password, "active": True,
                    "sip_uri_calling_preference": "disabled", "encode_contact_header_enabled": True,
                    "inbound": {"channel_limit": 1, "sip_subdomain_receive_settings": "only_my_connections"},
                    "outbound": {"outbound_voice_profile_id": profile_id, "channel_limit": 1}}
            rec = existing or self._call("POST", "/credential_connections", json=body)
            journal.finish("connection", connection_id=rec["id"])
        connection_id = journal.resources()["connection_id"]

        ref = f"ai-phone:{line_id}"
        st, data = journal.start("order", phone_number=number.e164, customer_reference=ref)
        if st != "done":
            existing = None
            if st == "started":  # the POST may have gone through: find the order by our customer_reference, or the delivered number
                for o in self._call("GET", "/number_orders", params={"filter[customer_reference]": ref}) or []:
                    if any(p.get("phone_number") == number.e164 for p in o.get("phone_numbers", [])):
                        existing = {"id": o["id"], "status": o.get("status", "pending")}
                        break
                if existing is None:
                    owned = self._call("GET", "/phone_numbers", params={"filter[phone_number]": number.e164}) or []
                    if owned:
                        existing = {"id": "reconciled", "status": "success"}
            if existing is None:
                self._exact_number(number.e164)
                rec = self._call("POST", "/number_orders", json={"phone_numbers": [{"phone_number": number.e164}], "connection_id": connection_id, "customer_reference": ref})
                journal.finish("order", order_id=rec["id"], order_status=rec.get("status", "pending"))
            else:
                journal.finish("order", order_id=existing["id"], order_status=existing["status"])
        return self.poll(journal)

    def poll(self, journal: Journal) -> ProvisionResult:
        res = journal.resources()
        e164 = next((s["planned"]["phone_number"] for s in journal.doc["steps"] if s["name"] == "order"), None)
        status = res.get("order_status")
        if res.get("order_id") not in (None, "reconciled") and status != "success":
            order = self._call("GET", f"/number_orders/{res['order_id']}")
            status = order.get("status", "pending")
            if status == "failure":
                journal.set_state("failed")
                return ProvisionResult("failed", f"Telnyx rejected the order for {e164}: {order.get('requirements_met', '')}".strip(), self._line(journal, e164, "failed"))
            if status != "success":
                journal.set_state("pending")
                why = "regulatory documents required (see the Telnyx portal → Numbers → Orders)" if order.get("requirements_met") is False else "order pending"
                return ProvisionResult("pending", f"{e164}: {why}; run `ai-phone lines resume` later", self._line(journal, e164, "pending"))
        if not res.get("number_id"):  # delivered (now or at order time): the number's own id is what release deletes
            owned = self._call("GET", "/phone_numbers", params={"filter[phone_number]": e164}) or []
            if not owned:  # the order says success but the number is not listed yet: not ready until it is
                journal.set_state("pending")
                return ProvisionResult("pending", f"{e164}: the order succeeded but the number is not listed on the account yet; run `ai-phone lines resume` in a minute",
                                       self._line(journal, e164, "pending"))
            journal.finish("order", order_id=res.get("order_id", ""), order_status="success", number_id=owned[0]["id"])
        journal.set_state("ready")
        return ProvisionResult("ready", f"{e164} is provisioned", self._line(journal, e164, "ready"))

    def _line(self, journal: Journal, e164: str | None, state: str) -> dict:
        """The line as far as it is known: a pending order still yields a loadable trunk line with the registrar and user."""
        r = journal.resources()
        sip_user = next((s["planned"].get("user_name") for s in journal.doc["steps"] if s["name"] == "connection"), None)
        line = {"kind": "trunk", "number": e164, "caller_id": e164, "registrar": REGISTRAR, "dial_domain": DIAL_DOMAIN,
                "register_via_proxy": True, "transport": "udp", "trusted_cidrs": list(SIGNALLING_CIDRS),
                "provider": {"name": self.name, "state": state,
                             "resources": {k: r[k] for k in ("outbound_voice_profile_id", "connection_id", "order_id", "number_id") if r.get(k)}}}
        if sip_user:
            line["user"] = sip_user
        return line

    def release(self, journal: Journal) -> ReleaseResult:
        r = journal.resources()
        leftover = {}
        for kind, key, path in (("number", "number_id", "/phone_numbers/"), ("connection", "connection_id", "/credential_connections/"),
                                ("profile", "outbound_voice_profile_id", "/outbound_voice_profiles/")):
            rid = r.get(key)
            if not rid:
                continue
            try:
                self._call("DELETE", f"{path}{rid}")
            except ProviderError as e:
                leftover[key] = f"{rid}: {e}"
        if leftover:
            journal.set_state("rollback_failed")
            return ReleaseResult(False, leftover)
        journal.set_state("pending")
        return ReleaseResult(True, {})

    # ---- caller id ----------------------------------------------------------------------------
    def start_caller_id_verification(self, e164: str) -> VerificationStep:
        self._call("POST", "/verified_numbers", json={"phone_number": e164, "verification_method": "sms"})
        return VerificationStep("enter_code_in_cli", f"Telnyx sent a 6-digit code by SMS to {e164}. Enter it here.", {"e164": e164})

    def complete_caller_id_verification(self, step: VerificationStep, code: str | None) -> bool:
        try:
            self._call("POST", f"/verified_numbers/{step.handle['e164']}/actions/verify", json={"verification_code": code or ""})
        except ProviderError as e:
            if "400" in str(e):
                return False
            raise
        return True
