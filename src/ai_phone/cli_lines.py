"""`ai-phone lines …`: buy, wire, resume, release, list, divert and verify. Carrier calls happen here, never in the daemon."""
from __future__ import annotations

import os
import time
from pathlib import Path

from ai_phone import paths
from ai_phone.config import E164_RE, LINE_ID_RE, ConfigError, line_password_env, load_config
from ai_phone.lines.config_writer import add_line, read_raw, remove_line, update_line, write_raw
from ai_phone.lines.credentials import generate_sip_password, generate_sip_user
from ai_phone.lines.forward import WHEN, divert_codes
from ai_phone.lines.journal import Journal
from ai_phone.secrets import SecretsError, load_secrets, remove_secret, write_secret

EXIT_OK, EXIT_PENDING, EXIT_ERROR = 0, 2, 3


def _say(msg: str) -> None:
    print(msg, flush=True)


def _ask(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def _env(name: str | None, what: str) -> str | None:
    if not name:
        return None
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"{what}: environment variable {name} is not set")
    return v


PROVIDER_CHOICES = ("telnyx", "twilio", "fake")  # mirrors ai_phone.providers.PROVIDERS without importing it at CLI start


def _provider(a):
    from ai_phone.providers import load_provider  # here, not at import: `ai-phone run` never loads the providers package
    kw = {}
    if a.provider == "fake":  # test hooks for the in-memory carrier
        kw["pending_polls"] = int(os.environ.get("AI_PHONE_FAKE_PENDING", "0"))
        kw["crash_after"] = os.environ.get("AI_PHONE_FAKE_CRASH_AFTER") or None
    return load_provider(a.provider, key=_env(a.key_env, "--key-env") or "", secret=_env(getattr(a, "secret_env", None), "--secret-env"), **kw)


def _config_path(a) -> Path:
    return Path(a.config).expanduser() if getattr(a, "config", None) else paths.config_path()


def _next_id(raw: dict, base: str) -> str:
    lines = raw.get("lines") or {}
    if base not in lines:
        return base
    n = 2
    while f"{base}-{n}" in lines:
        n += 1
    return f"{base}-{n}"


# ---- add <provider> ----------------------------------------------------------------------------

def _pick_number(provider, a):
    if a.number:
        return next((n for n in provider.search_numbers(a.country, None, 50) if n.e164 == a.number), None) or \
            _fail(f"{a.number} is not available from {provider.name}")
    nums = provider.search_numbers(a.country, a.area, 10)
    if not nums:
        _fail("no numbers found; try another --area or --country")
    for i, n in enumerate(nums, 1):
        cost = f"{n.monthly_cost:.2f} {n.currency}/month" if n.monthly_cost is not None else "price: see portal"
        _say(f"{i:2d}. {n.e164}   {cost}{'   (regulatory documents required)' if n.requires_regulatory else ''}")
    if a.yes:
        return nums[0]
    choice = input("Which one? [1] ").strip() or "1"
    return nums[int(choice) - 1]


def _fail(msg: str):
    _say(f"error: {msg}")
    raise SystemExit(EXIT_ERROR)


def provision_flow(provider, journal: Journal, config_path: Path, line_id: str, number, persona: str) -> int:
    journal.start("target", phone_number=number.e164, persona=persona)  # first thing in the journal, so resume always knows the number
    journal.finish("target")
    env_name = line_password_env(line_id)
    pw_step, planned = journal.start("credentials", user=generate_sip_user(line_id), password_env=env_name)
    if pw_step == "new":
        write_secret(env_name, generate_sip_password(), paths.secrets_path())
        journal.finish("credentials", **planned)
    creds = journal.resources() if pw_step == "done" else planned
    password = load_secrets(paths.secrets_path())[env_name]
    from ai_phone.providers.base import ProviderError
    try:
        result = provider.provision(journal, number, line_id, creds["user"], password)
    except ProviderError as e:
        _say(f"error: {e}\nThe journal is kept at {journal.path}; run `ai-phone lines resume {line_id}` to continue or `ai-phone lines remove {line_id}` to clean up.")
        return EXIT_ERROR
    return _commit(journal, config_path, line_id, result, persona, env_name)


def _commit(journal: Journal, config_path: Path, line_id: str, result, persona: str, env_name: str) -> int:
    if result.state == "failed":
        raw = read_raw(config_path)
        existing = (raw.get("lines") or {}).get(line_id)
        if existing is not None:  # a pending line whose order the carrier finally rejected: say so in the config too
            provider = dict(existing.get("provider") or {"name": journal.doc["provider"], "resources": {}})
            provider["state"] = "failed"
            update_line(config_path, line_id, provider=provider)
        _say(f"error: {result.message}\nRun `ai-phone lines remove {line_id} --key-env <NAME>` to release what was created.")
        return EXIT_ERROR
    if not result.line:  # nothing loadable to write yet; the journal keeps the state
        _say(f"{result.message}\nResume with: ai-phone lines resume {line_id} --key-env <NAME>")
        return EXIT_PENDING
    if result.state == "ready":
        journal.start("carrier_ready")
        journal.finish("carrier_ready")
    line = dict(result.line)
    line.update({"password_env": env_name, "persona": persona, "accept_invites_from": "registered_trunk"})
    line["provider"] = dict(line.get("provider") or {"name": journal.doc["provider"], "resources": {}})
    line["provider"]["state"] = result.state
    raw = read_raw(config_path)
    previous = (raw.get("lines") or {}).get(line_id)
    if previous is not None:
        update_line(config_path, line_id, **line)
    else:
        add_line(config_path, line_id, line)
    try:
        load_config(config_path)  # what we wrote must load, pending or ready
    except ConfigError as e:  # never leave an unloadable config behind: restore what was there, keep the journal
        if previous is not None:
            raw2 = read_raw(config_path)
            raw2["lines"][line_id] = previous
            write_raw(config_path, raw2)
        else:
            remove_line(config_path, line_id)
        _say(f"error: the provisioned line does not validate ({e}); the config was restored. The carrier resources exist: "
             f"fix the cause, then `ai-phone lines resume {line_id}`, or `ai-phone lines remove {line_id}` to release them.")
        return EXIT_ERROR
    if result.state == "ready":
        journal.start("local_committed")
        journal.finish("local_committed")  # the step's own timestamp records when; no result keys, so resources() stays carrier ids
    if result.state == "pending":
        _say(f"{result.message}\nLine {line_id} is written but pending. Resume with: ai-phone lines resume {line_id} --key-env <NAME>")
        return EXIT_PENDING
    number = line.get("number", line_id)
    if paths.socket_path().exists():
        _say(f"Line {line_id} is ready: {number}. Restart ai-phone to bring it live.")
    else:
        _say(f"Line {line_id} is ready: {number}. Run `ai-phone run`; it will register.")
    return EXIT_OK


def cmd_add(a) -> int:
    config_path = _config_path(a)
    if a.provider in ("trunk", "sip"):
        return _add_manual(a, config_path)
    provider = _provider(a)
    raw = read_raw(config_path)
    line_id = a.id or _next_id(raw, a.provider)
    if not LINE_ID_RE.fullmatch(line_id):
        _fail(f"--id must match {LINE_ID_RE.pattern}")
    if line_id in (raw.get("lines") or {}):
        _say(f"error: line {line_id!r} already exists (use --id to pick another name)")
        return EXIT_ERROR
    # everything the finished line will need is checked now, before a single carrier request spends money
    current = load_config(config_path) if config_path.exists() else None
    known_personas = set((current.personas if current else {}) | {"default": None})
    if a.persona not in known_personas:
        _fail(f"--persona {a.persona!r} is not defined in {config_path} (personas: {', '.join(sorted(known_personas))})")
    number = _pick_number(provider, a)
    cost = f"{number.monthly_cost:.2f} {number.currency}/month" if number.monthly_cost is not None else "the carrier's monthly price"
    if not _ask(f"Buy {number.e164} for {cost} on your {provider.name} account?", a.yes):
        return EXIT_ERROR
    journal = Journal.open(paths.lines_dir(), line_id, provider.name)
    return provision_flow(provider, journal, config_path, line_id, number, a.persona)


def _add_manual(a, config_path: Path) -> int:
    if not LINE_ID_RE.fullmatch(a.id or ""):
        _fail(f"--id is required and must match {LINE_ID_RE.pattern}")
    raw = read_raw(config_path)
    if a.id in (raw.get("lines") or {}):
        _fail(f"line {a.id!r} already exists; pick another --id")
    if a.provider == "trunk":
        if not E164_RE.fullmatch(a.number or ""):
            _fail("--number must be E.164, like +442079460000")
        if not a.registrar or not a.user or not a.password_env:
            _fail("--registrar, --user and --password-env are required for a trunk line")
        from ai_phone.config import host_of
        line = {"kind": "trunk", "number": a.number, "caller_id": a.caller_id or a.number, "registrar": a.registrar,
                "dial_domain": a.dial_domain or host_of(a.registrar), "user": a.user, "password_env": a.password_env,
                "transport": a.transport, "accept_invites_from": "registered_trunk", "trusted_cidrs": list(a.trusted_cidr or []),
                "persona": a.persona}
        if a.outbound_proxy:
            line["outbound_proxy"] = a.outbound_proxy
    else:
        line = {"kind": "sip", "user": a.user, "aor": f"sip:{a.user}@{a.host}", "caller_id": f"sip:{a.user}@{a.host}",
                "display_name": a.user, "transport": a.transport, "persona": a.persona,
                "accept_invites_from": "trusted" if a.trusted else "lan", "trusted_peers": list(a.trusted or [])}
    added = False
    try:
        add_line(config_path, a.id, line)
        added = True
        load_config(config_path)  # validate what we just wrote
    except (ValueError, ConfigError) as e:
        if added:  # only undo what this command wrote
            remove_line(config_path, a.id)
        _fail(str(e))
    _say(f"Line {a.id} added. Restart ai-phone (or start it) to bring it up.")
    return EXIT_OK


# ---- resume / remove / list ------------------------------------------------------------------

def _journal_for(line_id: str, a):
    raw = read_raw(_config_path(a))
    ln = (raw.get("lines") or {}).get(line_id) or {}
    pname = (ln.get("provider") or {}).get("name")
    jpath = paths.lines_dir() / f"{line_id}.journal.json"
    if pname is None and jpath.exists():
        pname = Journal.open(paths.lines_dir(), line_id).doc["provider"]
    return pname, ln


def _planned(journal: Journal, step: str, key: str):
    return next((s["planned"].get(key) for s in journal.doc["steps"] if s["name"] == step), None)


def cmd_resume(a) -> int:
    """Re-runs provisioning from the journal: finished steps are skipped, half-done ones reconciled, a pending order polled."""
    pname, ln = _journal_for(a.line_id, a)
    if not pname:
        _fail(f"line {a.line_id} has no provider journal to resume")
    a.provider = pname
    provider = _provider(a)
    journal = Journal.open(paths.lines_dir(), a.line_id, pname)
    e164 = (_planned(journal, "target", "phone_number")                      # journals from this version
            or _planned(journal, "order", "phone_number")                    # older Telnyx journals
            or _planned(journal, "number", "phone_number")                   # older Twilio journals
            or _planned(journal, "number", "name"))                          # older fake-carrier journals
    user = _planned(journal, "credentials", "user")
    env_name = ln.get("password_env") or _planned(journal, "credentials", "password_env") or line_password_env(a.line_id)
    if not e164 or not user:
        _fail(f"the journal for {a.line_id} has no number or credentials yet; run `ai-phone lines add` again or `lines remove --local-only`")
    password = load_secrets(paths.secrets_path()).get(env_name)
    if not password:
        _fail(f"{env_name} is missing from {paths.secrets_path()}; run `ai-phone lines remove {a.line_id} --local-only` and add the line again")
    from ai_phone.providers.base import Number, ProviderError
    try:
        result = provider.provision(journal, Number(e164, None, None, False, e164), a.line_id, user, password)
    except ProviderError as e:
        _say(f"error: {e}")
        return EXIT_ERROR
    return _commit(journal, _config_path(a), a.line_id, result, ln.get("persona") or _planned(journal, "target", "persona") or "default", env_name)


def cmd_remove(a) -> int:
    config_path = _config_path(a)
    pname, ln = _journal_for(a.line_id, a)
    if not ln and not pname:
        _fail(f"no line {a.line_id}")
    if pname and not a.local_only:
        if not a.key_env:
            _say(f"line {a.line_id} was provisioned on {pname}. Pass --key-env (and --secret-env for twilio) to release its carrier resources, "
                 f"or --local-only to remove it from this machine and keep them.")
            return EXIT_ERROR
        a.provider = pname
        provider = _provider(a)
        journal = Journal.open(paths.lines_dir(), a.line_id, pname)
        number = ln.get("number", a.line_id)
        if not _ask(f"Release {number} from your {pname} account? This cannot be undone.", a.yes):
            return EXIT_ERROR
        res = provider.release(journal)
        if not res.ok:
            _say("error: some resources could not be released; finish in the carrier portal:\n" + "\n".join(f"  {k}: {v}" for k, v in res.leftover.items()))
            update_line(config_path, a.line_id, provider={**(ln.get("provider") or {}), "state": "rollback_failed"})
            return EXIT_ERROR
        journal.delete()
    elif pname and a.local_only:
        res = Journal.open(paths.lines_dir(), a.line_id, pname).resources()
        _say("Kept in your carrier account (release them in the portal): " + ", ".join(f"{k}={v}" for k, v in res.items()))
        (paths.lines_dir() / f"{a.line_id}.journal.json").unlink(missing_ok=True)
    remove_line(config_path, a.line_id)
    env_name = ln.get("password_env") or line_password_env(a.line_id)
    if env_name.startswith("AI_PHONE_LINE_"):
        remove_secret(env_name, paths.secrets_path())
    _say(f"Line {a.line_id} removed. Restart ai-phone if it is running.")
    return EXIT_OK


def cmd_list(a) -> int:
    cfg = load_config(_config_path(a))
    if not cfg.lines:
        _say("no lines configured; try: ai-phone lines add telnyx --key-env TELNYX_API_KEY")
        return EXIT_OK
    for lid, ln in cfg.lines.items():
        where = ln.number or ln.aor
        prov = f" ({ln.provider.name}: {ln.provider.state})" if ln.provider else ""
        _say(f"{lid:12s} {ln.kind:5s} {where}{prov}  persona={ln.persona}")
    return EXIT_OK


# ---- forward / verify ------------------------------------------------------------------------

def cmd_forward(a) -> int:
    cfg = load_config(_config_path(a))
    ln = cfg.lines.get(a.line_id) or _fail(f"no line {a.line_id}")
    if ln.kind != "trunk" or not ln.number:
        _fail(f"line {a.line_id} has no phone number to divert to")
    if not E164_RE.fullmatch(a.from_number):
        _fail("--from must be your mobile number in E.164, like +447700900123")
    c = divert_codes(a.when, ln.number)
    _say(f"On your iPhone, open the dialler, type {c['activate']} and press call.\n"
         f"Check it with {c['check']}. Cancel this divert with {c['cancel']}; cancel every divert with {c['cancel_all']}.\n"
         f"(Settings → Phone → Call Forwarding only does 'all calls'. Some pay-as-you-go plans do not allow diverts.)")
    diverted = sorted(set(ln.diverted_from) | {a.from_number})
    update_line(_config_path(a), a.line_id, diverted_from=diverted)
    return EXIT_OK


def cmd_verify(a) -> int:
    config_path = _config_path(a)
    pname, ln = _journal_for(a.line_id, a)
    if not pname:
        _fail(f"line {a.line_id} was not provisioned by a carrier module; verify the number in your carrier's portal")
    if not E164_RE.fullmatch(a.number):
        _fail("number must be E.164")
    a.provider = pname
    provider = _provider(a)
    step = provider.start_caller_id_verification(a.number)
    _say(step.instructions)
    ok = False
    if step.kind == "enter_code_in_cli":
        for _ in range(3):
            code = input("Code: ").strip()
            if provider.complete_caller_id_verification(step, code):
                ok = True
                break
            _say("That code was not accepted.")
    else:
        deadline = time.time() + 120
        while time.time() < deadline and not ok:
            ok = provider.complete_caller_id_verification(step, None)
            if not ok:
                time.sleep(3)
    if not ok:
        _say("Verification did not complete.")
        return EXIT_ERROR
    verified = [v for v in (ln.get("verified_caller_ids") or []) if v.get("number") != a.number]
    verified.append({"number": a.number, "provider": pname, "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    update_line(config_path, a.line_id, verified_caller_ids=verified)
    _say(f"{a.number} can now be used as caller_id on line {a.line_id} (set `caller_id` in the config to use it). "
         "What the callee's handset shows is up to the carriers.")
    return EXIT_OK


def _guarded(fn):
    """Every lines command runs under the lines lock for its whole duration (config, secrets and journal writes
    included, so two commands never interleave), and provider/secrets failures are reported, never traced (exit 3)."""
    def run(a) -> int:
        from ai_phone.lines.journal import line_lock
        from ai_phone.providers.base import ProviderError
        try:
            with line_lock(paths.lines_dir()):
                return fn(a)
        except (ProviderError, SecretsError, ValueError) as e:
            _say(f"error: {e}")
            return EXIT_ERROR
    return run


# ---- argparse ------------------------------------------------------------------------------------

def register(sub) -> None:
    p = sub.add_parser("lines", help="phone numbers and SIP addresses this daemon answers on")
    p.add_argument("--config", default=None)
    s = p.add_subparsers(dest="lines_cmd", required=True)

    add = s.add_parser("add", help="add a line: telnyx | twilio | trunk | sip")
    add.add_argument("provider", choices=sorted(set(PROVIDER_CHOICES) | {"trunk", "sip"}))
    add.add_argument("--id")
    add.add_argument("--persona", default="default")
    add.add_argument("--yes", action="store_true")
    add.add_argument("--key-env"); add.add_argument("--secret-env")
    add.add_argument("--country", default="GB"); add.add_argument("--area"); add.add_argument("--number")
    add.add_argument("--registrar"); add.add_argument("--outbound-proxy"); add.add_argument("--dial-domain")
    add.add_argument("--user"); add.add_argument("--password-env"); add.add_argument("--caller-id")
    add.add_argument("--transport", choices=["udp", "tcp", "tls"], default="udp")
    add.add_argument("--trusted-cidr", action="append"); add.add_argument("--host"); add.add_argument("--trusted", action="append")
    add.set_defaults(func=_guarded(cmd_add))

    r = s.add_parser("resume"); r.add_argument("line_id"); r.add_argument("--key-env", required=True); r.add_argument("--secret-env"); r.set_defaults(func=_guarded(cmd_resume))
    rm = s.add_parser("remove"); rm.add_argument("line_id"); rm.add_argument("--key-env"); rm.add_argument("--secret-env")
    rm.add_argument("--local-only", action="store_true"); rm.add_argument("--yes", action="store_true"); rm.set_defaults(func=_guarded(cmd_remove))
    s.add_parser("list").set_defaults(func=_guarded(cmd_list))
    f = s.add_parser("forward"); f.add_argument("line_id"); f.add_argument("--when", choices=list(WHEN), default="no-answer")
    f.add_argument("--from", dest="from_number", required=True); f.set_defaults(func=_guarded(cmd_forward))
    v = s.add_parser("verify-caller-id"); v.add_argument("line_id"); v.add_argument("number"); v.add_argument("--key-env", required=True)
    v.add_argument("--secret-env"); v.set_defaults(func=_guarded(cmd_verify))
