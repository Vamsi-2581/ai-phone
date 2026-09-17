"""The setup conversation: which questions to ask, in what order, and what each answer writes. It runs in its own
thread and talks to the person only through a UI (ui.py); everything it changes goes through Actions (actions.py).
A step writes its answers only when the step completes, so leaving part-way never leaves half an answer behind.
The one exception is a number: once it is bought or connected, its line is written at once, because it exists at
the carrier."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

import ai_phone
from ai_phone import paths, voices
from ai_phone.config import E164_RE, HOST_RE, LINE_ID_RE, SIP_USER_RE
from ai_phone.pipeline.prompts import DISCLOSURE
from ai_phone.setup.actions import CARRIER_KEYS, ActionError, LineResult
from ai_phone.setup.ui import Back, Cancel, Check, Field, Option, Outcome, Page

WELCOME, BRAIN, LINE, VOICE, CALLERS, CHECK, DONE = range(7)
SAMPLE = "Hi, this is your agent. This is how I sound on the phone."
TIERS = (("t4", "Qwen 4B"), ("t9", "Qwen 9B"))
CARRIERS = (("twilio", "Twilio"), ("telnyx", "Telnyx"))
VOICE_GROUPS = (("af", "American · female"), ("am", "American · male"), ("bf", "British · female"), ("bm", "British · male"))
FORWARD_WHEN = ("no-answer", "busy", "unreachable", "all")
TRANSPORTS = ("udp", "tcp", "tls")


@dataclass
class Session:
    lines_added: list[str] = field(default_factory=list)
    summary: str = ""


def run(ui, actions) -> Outcome:
    session = Session()
    try:
        plan = welcome(ui, actions)
        i = 0
        while i < len(plan):
            ui.begin(plan[i])
            try:
                STEP[plan[i]](ui, actions, session)
                i += 1
            except Back as b:
                if not b.at_start:
                    continue  # a later question in the step: start the step again
                if i == 0:
                    plan, i = welcome(ui, actions), 0
                else:
                    i -= 1
        return Outcome(0, session.summary or done_text(actions, session))
    except Cancel:
        return Outcome(1, "Setup cancelled. What earlier steps saved is kept; nothing else was changed.")
    finally:
        ui.close()


# ---- answers ---------------------------------------------------------------------------------

def required(v: str) -> str | None:
    return None if v.strip() else "Enter a value."


def e164(v: str) -> str | None:
    return None if E164_RE.fullmatch(v.strip()) else "Use international format, like +15550100000."


def country(v: str) -> str | None:
    return None if re.fullmatch(r"[A-Za-z]{2}", v.strip()) else "Use a two-letter country code, like US or GB."


def area(v: str) -> str | None:
    return None if re.fullmatch(r"(\d{2,5})?", v.strip()) else "Use 2 to 5 digits, or leave it empty."


def sip_name(v: str) -> str | None:
    v = v.strip()
    ok = SIP_USER_RE.fullmatch(v) and LINE_ID_RE.fullmatch(v)
    return None if ok else "Use lowercase letters, digits and dashes."


def host(v: str) -> str | None:
    return None if HOST_RE.fullmatch(v.strip()) else "Use this Mac's hostname or IP address, like 192.0.2.10."


def url(v: str) -> str | None:
    return None if v.strip().startswith(("http://", "https://")) else "Use a URL that starts with http:// or https://."


def speed(v: str) -> str | None:
    try:
        x = float(v)
    except ValueError:
        x = math.nan
    return None if 0.5 <= x <= 2.0 else "Use a number from 0.5 to 2.0."


def count(v: str) -> str | None:
    return None if v.strip().isdecimal() else "Use a whole number: 0 or more."


# ---- words -----------------------------------------------------------------------------------

def price(n) -> str:
    if n.monthly_cost is None:
        return "the carrier's monthly price"
    symbol = {"USD": "$", "GBP": "£", "EUR": "€"}.get((n.currency or "").upper())
    if symbol:
        return f"{symbol}{n.monthly_cost:.2f}/month"
    return f"{n.monthly_cost:.2f} {n.currency}/month" if n.currency else f"{n.monthly_cost:.2f}/month"


def gb(n: int | None) -> str:
    return "several GB" if n is None else f"{n / 1e9:.1f} GB"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def speed_text(x: float) -> str:
    s = f"{x:g}"
    return s if "." in s else f"{s}.0"


def tilde(p: Path) -> str:
    """A path under the home directory, shown with ~ (never expanded)."""
    p, home = Path(p), Path.home()
    return "~/" + str(p.relative_to(home)) if p.is_relative_to(home) and p != home else str(p)


def home_dir() -> str:
    return tilde(paths.home())


def tier_name(tier: str) -> str:
    return dict(TIERS).get(tier, tier)


# ---- Welcome -----------------------------------------------------------------------------------

def welcome(ui, actions) -> list[int]:
    ui.begin(WELCOME)
    m = actions.machine()
    mem = m.memory_gb
    checks = [
        Check("SIP library", "built" if m.sip_library else "not built yet · run ./scripts/install.sh", m.sip_library),
        Check("Memory", f"{mem} GB · {'9B' if mem >= 32 else '4B'} model recommended", True if mem >= 16 else None),
        Check("Models", "not downloaded yet · setup can download them" if m.models_missing else "downloaded",
              None if m.models_missing else True),
        Check("Config", f"found · {home_dir()}/ (setup changes only what you pick)" if m.config_exists else f"new · {home_dir()}/", True),
    ]
    page = Page(WELCOME, "Give your AI a phone number", "Calls answered and placed by models running on this Mac.",
                note="Picks a line, a brain and a voice, then checks it all works.", checks=checks)
    if not m.config_exists:
        if ui.choose(page, [Option("Continue"), Option("Cancel", "exit setup, nothing is saved")], back=False) == 1:
            raise Cancel()
        return [BRAIN, LINE, VOICE, CALLERS, CHECK, DONE]
    plans = [[LINE], [BRAIN], [VOICE], [CALLERS], [BRAIN, LINE, VOICE, CALLERS]]
    i = ui.choose(page, [Option("Add a line", "a phone number, a SIP address or your own trunk"),
                         Option("Change the brain", "on this Mac, or your own endpoint"),
                         Option("Change the voice", "voice and speed"),
                         Option("Change what callers hear", "greeting, instructions, daily limit"),
                         Option("Run all steps"),
                         Option("Cancel", "exit setup, nothing is changed")], back=False)
    if i == len(plans):
        raise Cancel()
    return plans[i] + [CHECK, DONE]


# ---- Brain -------------------------------------------------------------------------------------

def brain(ui, actions, session: Session) -> None:
    cur = actions.current()
    where = ui.choose(Page(BRAIN, "Where should your agent think?",
                           "The model that writes what your agent says. Speech and voice always run on this Mac."),
                      [Option("On this Mac", "private · nothing leaves this Mac"),
                       Option("My own endpoint", "any OpenAI-compatible URL: LM Studio, llama.cpp, a hosted API")],
                      default=0 if cur.brain_kind == "local" else 1)
    if where == 1:
        return brain_endpoint(ui, actions, cur)
    mem = actions.machine().memory_gb
    recommended = "t9" if mem >= 32 else "t4"
    tiers = [t for t, _ in TIERS]
    default = tiers.index(cur.tier if cur.brain_kind == "local" and cur.tier in tiers else recommended)
    i = ui.choose(Page(BRAIN, "Which model size?", f"This Mac has {mem} GB of memory, so {tier_name(recommended)} is recommended."),
                  [Option("Qwen 4B", "fastest · best below 32 GB of memory"), Option("Qwen 9B", "smarter · needs 32 GB or more")],
                  default=default)
    actions.save_brain_local(tiers[i])


def brain_endpoint(ui, actions, cur) -> None:
    base_url, model, error = cur.base_url, cur.model, ""
    keep = "leave empty to keep the saved key" if cur.brain_key_saved else "leave empty for none"
    while True:
        page = Page(BRAIN, "Your endpoint", "Any server that speaks the OpenAI chat API. The key is optional and is saved "
                    f"to {home_dir()}/secrets.env.", error=error)
        base_url, model, key = (v.strip() for v in ui.ask(page, [
            Field("Base URL", base_url, validate=url),
            Field("Model", model, validate=required),
            Field("API key", "", secret=True, placeholder=keep),
        ]))
        try:
            where = urlsplit(base_url).hostname or base_url
        except ValueError:
            where = base_url
        try:
            ui.busy(replace(page, error=""), f"Sending one short test request to {where}…",
                    lambda: actions.test_endpoint(base_url, model, key or None))
            actions.save_brain_endpoint(base_url, model, key or None)
            return
        except ActionError as e:
            error = str(e)


# ---- Line --------------------------------------------------------------------------------------

def line(ui, actions, session: Session) -> None:
    cur = actions.current()
    i = ui.choose(Page(LINE, "How should people reach your agent?", "You can add more lines later by running setup again."),
                  [Option("A phone number", "Twilio or Telnyx · a new number or one you already have"),
                   Option("A SIP address", "free · softphones and other agents"),
                   Option("My own SIP trunk", "your PBX or provider"),
                   Option("Skip", "keep the lines you have" if cur.lines else "add one later")])
    if i == 0:
        phone_number(ui, actions, session)
    elif i == 1:
        sip_address(ui, actions, session)
    elif i == 2:
        own_trunk(ui, actions, session)


def phone_number(ui, actions, session: Session) -> None:
    carrier, name = CARRIERS[ui.choose(
        Page(LINE, "Which carrier?", "ai-phone uses your own account. Numbers and calls are billed by the carrier."),
        [Option("Twilio", "Account SID + Auth Token"), Option("Telnyx", "API key")])]
    creds = actions.saved_keys(carrier)
    page = Page(LINE, f"Use the saved {name} key?", f"It is in {home_dir()}/secrets.env.")
    if creds and ui.choose(page, [Option("Use the saved key"), Option("Enter a different key")]) == 1:
        creds = None
    owned, error = None, ""
    while owned is None:
        if not creds:
            page = Page(LINE, f"Your {name} credentials", f"Saved to {home_dir()}/secrets.env, readable only by you. "
                        "Never shown or sent anywhere else.", error=error)
            creds = dict(zip(CARRIER_KEYS[carrier], (v.strip() for v in ui.ask(page, credential_fields(carrier)))))
            page = replace(page, error="")
        try:
            owned = ui.busy(page, f"Checking your {name} account…", lambda: actions.owned_numbers(carrier, creds))
        except ActionError as e:
            creds, error = None, str(e)
    new = ui.choose(Page(LINE, "A new number or one you already have?",
                         checks=[Check(f"{name} key", f"works · {plural(len(owned), 'number')} on this account", True)]),
                    [Option("A new number", "search and buy one"), Option("One I already have", f"{len(owned)} on this account")]) == 0
    # a number that is already a line is not offered: connecting it again would overwrite its routing
    connected = {ln.get("number") for ln in actions.current().lines.values()}
    free = [n for n in owned if n.e164 not in connected]
    if not new and not free:
        ui.choose(Page(LINE, f"No other numbers on this {name} account" if owned else f"No numbers on this {name} account",
                       "Buy one instead, or add a number in the carrier's console and run setup again."),
                  [Option("Buy a new number")])
        new = True
    result = buy_number(ui, actions, carrier, name, creds) if new else attach_number(ui, actions, carrier, name, creds, free)
    if result.code != 0:
        ui.choose(Page(LINE, "The number is not ready yet" if result.code == 2 else "Something went wrong",
                       body="\n".join(result.messages)), [Option("Continue")], back=False)
        return
    session.lines_added.append(result.line_id)
    forward(ui, actions, result.line_id)


def credential_fields(carrier: str) -> list[Field]:
    if carrier == "twilio":
        return [Field("Account SID", secret=True, placeholder="AC…", validate=required),
                Field("Auth Token", secret=True, placeholder="32 characters", validate=required)]
    return [Field("API key", secret=True, placeholder="KEY…", validate=required)]


def buy_number(ui, actions, carrier: str, name: str, creds: dict):
    where, found, error = "US", [], ""
    while not found:
        page = Page(LINE, "Where should the number be?", "A two-letter country code, and an area code if you want one.", error=error)
        where, code = (v.strip() for v in ui.ask(page, [
            Field("Country", where, validate=country),
            Field("Area code", "", placeholder="optional", validate=area),
        ]))
        where = where.upper()
        try:
            found = ui.busy(replace(page, error=""), "Looking for numbers…",
                            lambda: actions.search_numbers(carrier, creds, where, code or None))
            error = "" if found else "No numbers found there. Try another area code, or leave it empty."
        except ActionError as e:
            error = str(e)
    n = found[ui.choose(Page(LINE, "Pick a number", f"Prices are {name}'s monthly price for the number. Calls are billed separately."),
                        [Option(n.e164, price(n) + (" · documents required" if n.requires_regulatory else "")) for n in found])]
    ui.choose(Page(LINE, f"Buy {n.e164} for {price(n)}?",
                   f"{name} bills your account every month until you remove the line (ai-phone lines remove)."),
              [Option(f"Buy {n.e164}", price(n))])  # the only way on is this option; Back starts the step again
    return run_line_action(ui, Page(LINE, f"Setting up {n.e164}"), f"Buying {n.e164} and connecting it…",
                           lambda: actions.buy_number(carrier, creds, n))


def attach_number(ui, actions, carrier: str, name: str, creds: dict, owned: list):
    shown = owned[:20]
    n = shown[ui.choose(Page(LINE, "Which number?", f"The numbers on your {name} account."), [Option(n.e164) for n in shown])]
    ui.choose(Page(LINE, f"Connect {n.e164} to ai-phone?",
                   "Its incoming calls will ring ai-phone. Nothing is bought. Removing the line later disconnects it and keeps the number."),
              [Option(f"Connect {n.e164}", "free")])
    return run_line_action(ui, Page(LINE, f"Setting up {n.e164}"), f"Connecting {n.e164}…",
                           lambda: actions.attach_number(carrier, creds, n))


def run_line_action(ui, page: Page, label: str, work) -> LineResult:
    try:
        return ui.busy(page, label, work)
    except ActionError as e:
        return LineResult("", 3, [str(e)])


def forward(ui, actions, line_id: str) -> None:
    """The line exists now, so Back inside these questions returns to the first one, never to the purchase."""
    while True:
        try:
            if ui.choose(Page(LINE, "Also forward your own mobile to it?",
                              "Calls to your mobile can ring your agent when you are busy or don't answer. "
                              "Your mobile carrier's divert charges apply."),
                         [Option("No"), Option("Yes")], back=False) == 0:
                return
            (mobile,) = ui.ask(Page(LINE, "Your mobile number", "In international format, like +447700900123."),
                               [Field("Mobile number", validate=e164)])
            when = FORWARD_WHEN[ui.choose(Page(LINE, "When should calls go to your agent?"),
                                          [Option("When I don't answer"), Option("When I'm busy"),
                                           Option("When my phone is off"), Option("All calls")])]
            break
        except Back:
            continue
    codes = actions.forward(line_id, mobile.strip(), when)
    body = (f"Turn on     {codes['activate']}\nCheck       {codes['check']}\nTurn off    {codes['cancel']}\n"
            f"Turn off every divert   {codes['cancel_all']}")
    ui.choose(Page(LINE, "Dial this on your mobile", "Open the phone app, type the code and press call.", body=body),
              [Option("Done")], back=False)


def sip_address(ui, actions, session: Session) -> None:
    name, address, error = "agent", actions.lan_address(), ""
    while True:
        name, address = (v.strip() for v in ui.ask(
            Page(LINE, "Name your SIP address", "Softphones and other agents call sip:<name>@<this Mac>. Free; no carrier.", error=error),
            [Field("Name", name, validate=sip_name), Field("This Mac's address", address, validate=host)]))
        try:
            session.lines_added.append(actions.add_sip_line(name, address))
            return
        except ActionError as e:
            error = str(e)


def own_trunk(ui, actions, session: Session) -> None:
    registrar = user = number = error = ""
    while True:
        registrar, user, password, number = (v.strip() for v in ui.ask(
            Page(LINE, "Your SIP trunk", "The details your PBX or provider gave you. The password is saved to "
                 f"{home_dir()}/secrets.env.", error=error),
            [Field("Registrar", registrar, placeholder="sip:sip.example.com", validate=required),
             Field("Username", user, validate=required),
             Field("Password", secret=True, validate=required),
             Field("Your number on it", number, placeholder="+15550100000", validate=e164)]))
        transport = TRANSPORTS[ui.choose(Page(LINE, "Which transport?", "Use what your provider says; UDP is the usual one."),
                                         [Option("UDP"), Option("TCP"), Option("TLS", "encrypted signalling")])]
        uri = registrar if registrar.startswith("sip:") else f"sip:{registrar}"
        try:
            session.lines_added.append(actions.add_trunk_line(uri, user, password, number, transport))
            return
        except ActionError as e:
            error = str(e)


# ---- Voice -------------------------------------------------------------------------------------

def voice(ui, actions, session: Session) -> None:
    cur = actions.current()
    names = [v for v in actions.voices() if v[:2] in dict(VOICE_GROUPS)]
    if not names:
        i = ui.choose(Page(VOICE, "How should your agent sound?",
                           "The speech model is not downloaded yet, so there are no voices to pick from."),
                      [Option("Download it now", "about 0.4 GB"), Option("Keep the default voice", "Heart · American · female")])
        if i == 0:
            try:
                ui.progress(Page(VOICE, "Downloading the speech model"), "Downloading…", actions.pull_voice_model)
                names = [v for v in actions.voices() if v[:2] in dict(VOICE_GROUPS)]
            except ActionError:
                names = []
        if not names:
            actions.save_voice(voices.DEFAULT, 1.0)
            return
    groups = [(label, [v for v in names if v.startswith(prefix)]) for prefix, label in VOICE_GROUPS]
    groups = [(label, vs) for label, vs in groups if vs]
    default = next((i for i, (_, vs) in enumerate(groups) if cur.voice in vs), 0)
    label, group = groups[ui.choose(
        Page(VOICE, "How should your agent sound?", "English voices that came with the speech model. You can listen before you pick."),
        [Option(label, plural(len(vs), "voice")) for label, vs in groups], default=default)]
    v = group[ui.choose(Page(VOICE, label, "Pick one to hear it or use it."),
                        [Option(voices.title(v), "current" if v == cur.voice else "") for v in group],
                        default=group.index(cur.voice) if cur.voice in group else 0)]
    error = ""
    while True:
        page = Page(VOICE, voices.title(v), voices.describe(v), error=error)
        if ui.choose(page, [Option("Use this voice"), Option("Hear it", "plays a short sample on this Mac's speakers")]) == 0:
            break
        try:
            ui.busy(replace(page, error=""), "Playing…", lambda: actions.play_sample(v, cur.speed, SAMPLE))
            error = ""
        except ActionError as e:
            error = str(e)
    (value,) = ui.ask(Page(VOICE, "How fast should it talk?", "1.0 is normal. 0.9 is a little slower, 1.1 a little faster."),
                      [Field("Speed", speed_text(cur.speed), validate=speed)])
    actions.save_voice(v, float(value))


# ---- Callers -----------------------------------------------------------------------------------

def callers(ui, actions, session: Session) -> None:
    cur = actions.current()
    greeting, instructions, limit, error = cur.greeting, cur.instructions, str(cur.daily_limit), ""
    while True:
        greeting, instructions, limit = (v.strip() for v in ui.ask(
            Page(CALLERS, "What should callers hear?", "The greeting plays when ai-phone answers. The instructions tell "
                 "ai-phone's own model what to do when your AI isn't listening.", error=error),
            [Field("Greeting", greeting, validate=required),
             Field("Instructions", instructions, validate=required),
             Field("Outbound calls per day", limit, placeholder="0 means no limit", validate=count)]))
        try:
            actions.save_callers(greeting, instructions, int(limit))
            return
        except ActionError as e:
            error = str(e)


# ---- Check -------------------------------------------------------------------------------------

def check(ui, actions, session: Session) -> None:
    while True:
        rows: list[Check] = []
        rows.append(check_models(ui, actions, checking(rows)))
        error = actions.check_config()
        rows.append(Check("Config", "loads", True) if error is None else Check("Config", error, False))
        cur = actions.current()
        if error is None:
            rows.extend(check_lines(ui, actions, checking(rows), cur))
        if actions.voices():  # the speech model is here
            try:
                ui.busy(checking(rows), "Playing your agent's voice…", lambda: actions.play_sample(cur.voice, cur.speed, DISCLOSURE))
                rows.append(Check("Voice", f"{voices.title(cur.voice)} · played", True))
            except ActionError as e:
                rows.append(Check("Voice", str(e), False))
        fine = all(r.ok is True for r in rows)
        if ui.choose(Page(CHECK, "Everything works" if fine else "A few things need attention", checks=list(rows)),
                     [Option("Continue"), Option("Check again")]) == 0:
            return


def checking(rows: list[Check]) -> Page:
    """The page shown while a check runs: the rows found so far."""
    return Page(CHECK, "Checking that it all works", checks=list(rows))


def check_models(ui, actions, page: Page) -> Check:
    if not actions.missing_models():
        return Check("Models", "downloaded", True)
    size = ui.busy(page, "Checking the download size…", actions.download_size)
    later = ui.choose(Page(CHECK, "Download the models now?", f"About {gb(size)} into the Hugging Face cache "
                           "(~/.cache/huggingface). ai-phone needs them to answer calls."),
                      [Option("Download now", gb(size)), Option("Later", "run: uv run ai-phone models pull")]) == 1
    if later:
        return Check("Models", "not downloaded yet · uv run ai-phone models pull", None)
    try:
        ui.progress(Page(CHECK, "Downloading the models", "You can leave this running. It picks up where it stopped if interrupted."),
                    "Downloading…", actions.pull_models)
        return Check("Models", "downloaded", True)
    except ActionError as e:
        return Check("Models", str(e), False)


def check_lines(ui, actions, page: Page, cur) -> list[Check]:
    found = ui.busy(page, "Checking your lines… (up to 20 seconds)", actions.check_lines) if cur.lines else []
    if not found:
        return [Check("Lines", "none yet · run setup again to add one", None)]
    ok = {"registered": True, "ready": True, "registering": None, "skipped": None}
    return [Check("Lines" if line_id == "lines" else f"Line {line_id}", state + (f" · {detail}" if detail else ""), ok.get(state, False))
            for line_id, state, detail in found]


# ---- Done --------------------------------------------------------------------------------------

def done(ui, actions, session: Session) -> None:
    session.summary = done_text(actions, session)
    try:
        ui.choose(Page(DONE, "You're set up", "Everything is saved. Here's how to use it.", body=session.summary),
                  [Option("Finish")], back=False)
    except Cancel:
        pass  # everything is saved already: leaving here is finishing


def done_text(actions, session: Session) -> str:
    cur = actions.current()
    config, secrets = tilde(paths.config_path()), tilde(paths.secrets_path())
    w = max(len(config), len(secrets)) + 5
    out = ["Saved",
           f"  {config.ljust(w)}lines, brain, voice, what callers hear",
           f"  {secrets.ljust(w)}keys and passwords, readable only by you",
           "",
           "Lines"]
    rows = [(line_id, ln.get("number") or ln.get("aor") or "", line_kind(ln)) for line_id, ln in cur.lines.items()]
    if rows:
        a, b = (max(len(r[i]) for r in rows) for i in (0, 1))
        out += [f"  {i.ljust(a)}   {where.ljust(b)}   {kind}" for i, where, kind in rows]
    else:
        out.append("  none yet · run setup again to add one")
    brain_row = (f"on this Mac · {tier_name(cur.tier)}" if cur.brain_kind == "local" else f"{cur.base_url} · {cur.model}")
    out += [f"Brain    {brain_row}",
            f"Voice    {voices.title(cur.voice)} · {voices.describe(cur.voice)} · {speed_text(cur.speed)}×",
            "",
            "Start ai-phone",
            "  uv run ai-phone run",
            "",
            "Try it"]
    tries = []
    for ln in cur.lines.values():
        if ln.get("kind") == "sip":
            user = ln.get("user") or (ln.get("aor") or "sip:agent@").split(":", 1)[-1].split("@", 1)[0]
            tries += [f"  pjsua --local-port 5070 sip:{user}@127.0.0.1",
                      "    talk to your agent through this Mac's mic and speakers (free)"]
        elif ln.get("number"):
            tries.append(f"  call {ln['number']} from any phone (your normal call rates)")
    out += tries or ["  add a line first: uv run ai-phone setup"]
    repo = tilde(Path(ai_phone.__file__).parents[2])
    out += ["",
            "Give your AI the phone",
            '  Tell your AI: "You can make and take phone calls with the ai-phone command,',
            f'  run from {repo}: `uv run ai-phone call +15550100000 --prompt "…" --json`',
            "  lets ai-phone's own agent talk and returns the transcript; `uv run ai-phone call … --relay --json`,",
            '  then `ai-phone listen --json` and `ai-phone say "…"`, lets you talk yourself; `ai-phone listen --json`',
            '  waits for incoming calls; `ai-phone hangup` ends a call."']
    return "\n".join(out)


def line_kind(ln: dict) -> str:
    kind = ln.get("kind", "trunk")
    provider = ln.get("provider") or {}
    if provider.get("name"):
        kind += f" · {provider['name']}"
        if provider.get("state", "ready") != "ready":
            kind += f" · {provider['state']}"
    return kind


STEP = {BRAIN: brain, LINE: line, VOICE: voice, CALLERS: callers, CHECK: check, DONE: done}
