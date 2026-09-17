"""Everything the setup flow changes or checks. The flow never writes a file or calls a carrier itself; it calls these.
A failure the person can fix raises ActionError with a message that is already safe to show (no secrets).

Config writes take the lines lock, change one section, write, and load the result; a config that no longer loads is
put back as it was and the ConfigError becomes the ActionError."""
from __future__ import annotations

import copy
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx
import yaml

import ai_phone
from ai_phone import models, paths, voices
from ai_phone.cli_lines import manual_line, next_line_id, provision_flow, write_line
from ai_phone.config import Config, ConfigError, line_password_env, load_config
from ai_phone.control.client import probe
from ai_phone.lines.config_writer import read_raw, write_raw
from ai_phone.lines.forward import divert_codes
from ai_phone.lines.journal import Journal, line_lock
from ai_phone.secrets import SecretsError, load_secrets, redact, remove_secret, write_secret
from ai_phone.telephony.state import StateError, StateFile

CARRIER_KEYS = {"twilio": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"), "telnyx": ("TELNYX_API_KEY",), "fake": ("FAKE_API_KEY",)}
BRAIN_KEY_ENV = "AI_PHONE_BRAIN_API_KEY"  # a secret name by the daemon's rules (it contains KEY), so workers never inherit it
VENDOR = Path(ai_phone.__file__).parent / "vendor"  # where install.sh puts the PJSIP binding
DAEMON = [sys.executable, "-c", "import sys; from ai_phone.cli import main; sys.exit(main(sys.argv[1:]))", "run", "--fake"]
CHECK_SEC, POLL_SEC, STOP_SEC = 20, 0.5, 10  # how long a trial start may take to register, how often to look, how long to stop
SAMPLE_RATE = 24000  # Kokoro's own rate
LOOPBACK = ("127.0.0.1", "localhost", "::1")  # the config's rule: plain http only to this machine


@dataclass
class Machine:
    sip_library: bool           # the PJSIP binding is built into src/ai_phone/vendor
    memory_gb: int
    models_missing: list[str]   # models still to download for the current config (defaults when there is none)
    config_exists: bool


@dataclass
class Current:
    """What the config says now (defaults for a new config)."""
    exists: bool
    lines: dict[str, dict]      # raw line entries by id
    brain_kind: str             # "local" | "openai_compatible"
    tier: str                   # resolved: "t4" | "t9"
    base_url: str
    model: str
    brain_key_saved: bool       # the brain's api_key_env has a value in secrets.env
    voice: str
    speed: float
    greeting: str
    instructions: str
    daily_limit: int            # calls.daily_limit, or 20 when the config has none


@dataclass
class LineResult:
    line_id: str
    code: int                   # cli_lines EXIT_OK / EXIT_PENDING / EXIT_ERROR
    messages: list[str]


class ActionError(RuntimeError):
    pass


def sip_library_built() -> bool:
    return any(VENDOR.glob("_pjsua2*.so"))


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    return dict(value) if isinstance(value, dict) else {}


def _saved() -> dict[str, str]:
    try:
        return load_secrets()
    except SecretsError:
        return {}


class Actions:
    def __init__(self, config_path: Path, player=None, provider_loader=None, http: httpx.Client | None = None):
        self.config_path = Path(config_path)
        self.player = player or KokoroPlayer()
        self.provider_loader = provider_loader
        self.http = http  # tests pass a client with a mock transport

    # ---- what is there ---------------------------------------------------------------------------

    def machine(self) -> Machine:
        cur = self.current()
        return Machine(sip_library=sip_library_built(), memory_gb=models.memory_gb(),
                       models_missing=models.missing(cur.tier, cur.brain_kind), config_exists=cur.exists)

    def current(self) -> Current:
        exists = self.config_path.exists()
        raw, cfg = {}, Config()
        if exists:
            try:
                raw = read_raw(self.config_path)
                cfg = load_config(self.config_path)
            except (ConfigError, yaml.YAMLError, OSError):
                pass  # the Check step shows why; setup still runs, so the person can change what is wrong
        raw = raw if isinstance(raw, dict) else {}
        lines = {k: dict(v) for k, v in _section(raw, "lines").items() if isinstance(v, dict)}
        limit = _section(raw, "calls").get("daily_limit", 20)
        limit = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit >= 0 else 20
        key_env = cfg.brain.api_key_env
        return Current(exists=exists, lines=lines, brain_kind=cfg.brain.kind, tier=models.resolve_tier(cfg.models.tier),
                       base_url=cfg.brain.base_url, model=cfg.brain.model, brain_key_saved=bool(key_env and _saved().get(key_env)),
                       voice=cfg.tts.voice, speed=cfg.tts.speed, greeting=cfg.inbound.greeting,
                       instructions=cfg.inbound.instructions, daily_limit=limit)

    def _read(self) -> dict:
        """The raw config for a change. A file that is not a YAML mapping is never overwritten."""
        try:
            raw = read_raw(self.config_path)
        except yaml.YAMLError:
            raise ActionError(f"{self.config_path.name} is not valid YAML; fix it or move it away, then run setup again") from None
        if not isinstance(raw, dict):
            raise ActionError(f"{self.config_path.name} must be a mapping; fix it or move it away, then run setup again")
        return raw

    def _edit(self, change: Callable[[dict], None]) -> None:
        with line_lock(paths.lines_dir()):
            existed = self.config_path.exists()
            raw = self._read()
            before = copy.deepcopy(raw)
            change(raw)
            write_raw(self.config_path, raw)
            try:
                load_config(self.config_path)
            except ConfigError as e:
                if existed:
                    write_raw(self.config_path, before)
                else:
                    self.config_path.unlink()
                raise ActionError(str(e)) from None

    # ---- brain -----------------------------------------------------------------------------------

    def save_brain_local(self, tier: str) -> None:
        def change(raw: dict) -> None:
            raw["brain"] = {"kind": "local"}
            raw["models"] = {**_section(raw, "models"), "tier": tier}
        self._edit(change)

    def _brain_key_env(self) -> str | None:
        """The key variable the current endpoint brain uses, if any."""
        try:
            brain = _section(self._read(), "brain")
        except ActionError:
            return None
        env = brain.get("api_key_env")
        return env if brain.get("kind") == "openai_compatible" and isinstance(env, str) else None

    def test_endpoint(self, url: str, model: str, key: str | None) -> str:
        try:
            parts = urlsplit(url)
            host = parts.hostname or url
        except ValueError:
            parts, host = None, url
        if parts is not None and parts.scheme == "http" and parts.hostname not in LOOPBACK:
            raise ActionError("a brain that is not on this machine must use https")  # before a key can cross the network
        if key is None:
            env = self._brain_key_env()
            key = _saved().get(env) if env else None
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        body = {"model": model, "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 5}
        try:
            r = (self.http or httpx).post(f"{url.rstrip('/')}/chat/completions", json=body, headers=headers, timeout=15)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
            raise ActionError(f"could not reach {host}: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                said = r.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                said = r.text
            said = redact(" ".join(str(said).split()), [key])[:160]
            raise ActionError(f"the endpoint said {r.status_code}: {said}")
        try:
            return r.json()["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ActionError(f"{host} answered, but not like an OpenAI chat API (is the URL right? it usually ends in /v1)") from None

    def save_brain_endpoint(self, url: str, model: str, key: str | None) -> None:
        env = BRAIN_KEY_ENV if key else self._brain_key_env()
        previous = _saved().get(BRAIN_KEY_ENV)

        def change(raw: dict) -> None:
            brain = {k: v for k, v in _section(raw, "brain").items() if k not in ("kind", "base_url", "model", "api_key_env")}
            brain = {"kind": "openai_compatible", "base_url": url, "model": model, **brain}
            if env:
                brain["api_key_env"] = env
            raw["brain"] = brain

        try:
            if key:
                write_secret(BRAIN_KEY_ENV, key)
            self._edit(change)
        except (ActionError, SecretsError) as e:
            if key:  # put back the key that was there
                try:
                    if previous:
                        write_secret(BRAIN_KEY_ENV, previous)
                    else:
                        remove_secret(BRAIN_KEY_ENV)
                except SecretsError:
                    pass
            raise ActionError(redact(str(e), [key])) from None

    # ---- carriers --------------------------------------------------------------------------------

    def saved_keys(self, carrier: str) -> dict[str, str] | None:
        saved = _saved()
        keys = {name: saved.get(name) for name in CARRIER_KEYS[carrier]}
        return keys if all(keys.values()) else None

    def _provider(self, carrier: str, creds: dict[str, str]):
        from ai_phone.providers.base import ProviderError
        loader = self.provider_loader
        if loader is None:
            from ai_phone.providers import load_provider as loader
        names = CARRIER_KEYS[carrier]
        try:
            return loader(carrier, key=creds[names[0]], secret=creds.get(names[1]) if len(names) > 1 else None)
        except ProviderError as e:
            raise ActionError(redact(f"That key did not work: {e}", creds.values())) from None

    def _carrier_error(self, carrier: str, creds: dict[str, str], e, otherwise: str) -> ActionError:
        """A request that never got an answer (or a carrier outage) is not the key's fault."""
        text = f"Could not reach {carrier.capitalize()}: {e}" if e.retryable else f"{otherwise}: {e}"
        return ActionError(redact(text, creds.values()))

    def owned_numbers(self, carrier: str, creds: dict[str, str]) -> list:
        from ai_phone.providers.base import ProviderError
        try:
            return self._provider(carrier, creds).owned_numbers()
        except ProviderError as e:
            raise self._carrier_error(carrier, creds, e, "That key did not work") from None

    def search_numbers(self, carrier: str, creds: dict[str, str], country: str, area: str | None) -> list:
        from ai_phone.providers.base import ProviderError
        try:
            return self._provider(carrier, creds).search_numbers(country, area, 9)
        except ProviderError as e:
            raise self._carrier_error(carrier, creds, e, "Could not search for numbers") from None

    def _line_id(self, raw: dict, carrier: str, number, existing: bool) -> str:
        """twilio, twilio-2, …: the first id with neither a line nor a journal, or the id of an unfinished attempt to set
        up this same number the same way (bought, or brought), which provisioning then picks up where it stopped. Any
        other journal belongs to `lines resume` / `lines remove` and is left alone."""
        lines = _section(raw, "lines")
        n = 1
        while True:
            line_id = carrier if n == 1 else f"{carrier}-{n}"
            n += 1
            if line_id in lines:
                continue
            target = _journal_target(paths.lines_dir() / f"{line_id}.journal.json")
            if target is None or target == (number.e164, existing):
                return line_id

    def _connect(self, carrier: str, creds: dict[str, str], number, existing: bool) -> LineResult:
        provider = self._provider(carrier, creds)
        messages: list[str] = []
        try:
            for name, value in creds.items():
                write_secret(name, value)
            with line_lock(paths.lines_dir()):
                raw = self._read()
                line_id = self._line_id(raw, carrier, number, existing)
                if self.config_path.exists():
                    load_config(self.config_path)  # the line is written into this config: it must load before anything is bought
                journal = Journal.open(paths.lines_dir(), line_id, provider.name)
                code = provision_flow(provider, journal, self.config_path, line_id, number, existing=existing, say=messages.append)
        except (RuntimeError, ValueError, OSError) as e:  # ConfigError, SecretsError, ProviderError, a journal problem
            raise ActionError(redact(str(e), creds.values())) from None
        return LineResult(line_id, code, [redact(m, creds.values()) for m in messages])

    def buy_number(self, carrier: str, creds: dict[str, str], number) -> LineResult:
        return self._connect(carrier, creds, number, existing=False)

    def attach_number(self, carrier: str, creds: dict[str, str], number) -> LineResult:
        return self._connect(carrier, creds, number, existing=True)

    def forward(self, line_id: str, mobile: str, when: str) -> dict:
        with line_lock(paths.lines_dir()):
            line = _section(self._read(), "lines").get(line_id)
            number = line.get("number") if isinstance(line, dict) else None
            if not number:
                raise ActionError(f"line {line_id} has no phone number to divert to")
            codes = divert_codes(when, number)

            def change(raw: dict) -> None:
                ln = raw["lines"][line_id]
                ln["diverted_from"] = sorted(set(ln.get("diverted_from") or []) | {mobile})
            self._edit(change)
        return codes

    # ---- lines by hand ---------------------------------------------------------------------------

    def lan_address(self) -> str:
        """The address other devices reach this Mac on: the source address of the default route (nothing is sent)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))
            address = s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()
        return address if address != "0.0.0.0" else "127.0.0.1"

    def add_sip_line(self, name: str, host: str) -> str:
        with line_lock(paths.lines_dir()):
            raw = self._read()
            # two SIP lines with one address: the endpoint gives every call to the first, so the other never rings
            for other, ln in _section(raw, "lines").items():
                if not isinstance(ln, dict) or ln.get("kind") != "sip":
                    continue
                aor = str(ln.get("aor") or "")
                if name == aor.removeprefix("sip:").split("@", 1)[0]:
                    raise ActionError(f"{aor} is already line {other}; pick another name")
                if name == ln.get("user"):
                    raise ActionError(f"sip:{name}@… is already line {other}; pick another name")
            line_id = next_line_id(raw, name)
            try:
                write_line(self.config_path, line_id, manual_line("sip", user=name, host=host))
            except ValueError as e:  # a ConfigError is a ValueError too
                raise ActionError(str(e)) from None
        return line_id

    def add_trunk_line(self, registrar: str, user: str, password: str, number: str, transport: str) -> str:
        with line_lock(paths.lines_dir()):
            line_id = next_line_id(self._read(), "trunk")
            env = line_password_env(line_id)
            try:
                write_secret(env, password)
                write_line(self.config_path, line_id, manual_line("trunk", number=number, registrar=registrar, user=user,
                                                                  password_env=env, transport=transport))
            except (ValueError, SecretsError) as e:
                try:
                    remove_secret(env)
                except SecretsError:
                    pass
                raise ActionError(redact(str(e), [password])) from None
        return line_id

    # ---- voice -----------------------------------------------------------------------------------

    def voices(self) -> list[str]:
        return voices.installed_voices()

    def play_sample(self, voice: str, speed: float, text: str) -> None:
        try:
            self.player.play(voice, speed, text)
        except ActionError:
            raise
        except Exception as e:  # the audio stack can fail in many ways; none of them should end setup
            raise ActionError(f"could not play the sample: {type(e).__name__}: {e}"[:200]) from None

    def pull_voice_model(self, report: Callable[[int, int], None]) -> None:
        try:
            models.pull([models.TTS], report)
        except models.ModelsError as e:
            raise ActionError(str(e)) from None

    def save_voice(self, voice: str, speed: float) -> None:
        def change(raw: dict) -> None:
            raw["tts"] = {**_section(raw, "tts"), "voice": voice, "speed": speed}
        self._edit(change)

    # ---- callers ---------------------------------------------------------------------------------

    def save_callers(self, greeting: str, instructions: str, daily_limit: int) -> None:
        def change(raw: dict) -> None:
            raw["inbound"] = {**_section(raw, "inbound"), "greeting": greeting, "instructions": instructions}
            raw["calls"] = {**_section(raw, "calls"), "daily_limit": daily_limit}
        self._edit(change)

    # ---- check -----------------------------------------------------------------------------------

    def missing_models(self) -> list[str]:
        return self.machine().models_missing

    def download_size(self) -> int | None:
        return models.download_size(self.missing_models())

    def pull_models(self, report: Callable[[int, int], None]) -> None:
        try:
            models.pull(self.missing_models(), report)
        except models.ModelsError as e:
            raise ActionError(str(e)) from None

    def check_config(self) -> str | None:
        try:
            load_config(self.config_path)
        except ConfigError as e:
            return str(e)
        return None

    def check_lines(self) -> list[tuple[str, str, str]]:
        """(line id, state, detail) for each line: from the running ai-phone, or from a short trial start of it."""
        try:
            running = probe(paths.socket_path())
        except RuntimeError as e:  # socket_path refuses an unsafe directory under /tmp (a symlink, another owner)
            return [("lines", "failed", str(e))]
        if running:
            try:
                return _line_rows(StateFile.read(paths.state_path()))
            except (StateError, ValueError):
                return [("lines", "failed", "ai-phone is running but has not written its status yet")]
        if not sip_library_built():
            return [("lines", "skipped", "SIP library not built · run ./scripts/install.sh")]
        return self._trial_start()

    def _trial_start(self) -> list[tuple[str, str, str]]:
        """Start `ai-phone run --fake` (no models load) until every trunk has an answer from its registrar or CHECK_SEC
        pass, then stop it. Only a state.json written by this start is read."""
        started = time.time()
        doc = None
        with tempfile.TemporaryFile() as log:
            try:
                proc = subprocess.Popen(DAEMON, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            except OSError as e:
                return [("lines", "failed", f"could not start ai-phone: {e.strerror or e}")]
            try:
                while True:
                    if proc.poll() is not None:
                        return [("lines", "failed", _last_line(log))]
                    doc = _fresh_state(started) or doc
                    if doc is not None and _settled(doc):
                        break
                    if time.time() - started >= CHECK_SEC:
                        break
                    time.sleep(POLL_SEC)
            finally:
                _stop(proc)
        if doc is None:
            return [("lines", "registering", f"ai-phone did not report its lines within {CHECK_SEC:g} seconds")]
        return _line_rows(doc)


def _journal_target(path: Path) -> tuple[str | None, bool | None] | None:
    """(number, brought) of the setup a journal records; None when it records none. Unreadable: ('?', None)."""
    if not os.path.lexists(path):
        return None
    try:
        if path.is_symlink():
            raise ValueError("a symlink")
        steps = json.loads(path.read_text())["steps"]
        if not steps:  # Journal.open() writes an empty one; it holds nothing
            return None
        planned = next(s["planned"] for s in steps if s["name"] == "target")
        return planned.get("phone_number"), bool(planned.get("brought"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
        return "?", None


def _fresh_state(since: float) -> dict | None:
    try:
        doc = StateFile.read(paths.state_path())
    except (StateError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("written_at", 0) >= since else None


def _settled(doc: dict) -> bool:
    return not any(ln.get("kind") == "trunk" and ln.get("state") in ("registering", "unregistered")
                   for ln in (doc.get("lines") or {}).values())


def _line_rows(doc: dict) -> list[tuple[str, str, str]]:
    rows = []
    for line_id, ln in (doc.get("lines") or {}).items():
        state = ln.get("state", "")
        if ln.get("kind") == "sip":
            rows.append((line_id, "ready", "listening"))
        elif state in ("registered", "registering"):
            rows.append((line_id, state, ""))
        else:
            rows.append((line_id, state, ln.get("last_error") or ""))
    return rows


def _stop(proc) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=STOP_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _last_line(log) -> str:
    log.seek(0)
    lines = [ln.strip() for ln in log.read().decode(errors="replace").splitlines() if ln.strip()]
    text = lines[-1] if lines else "ai-phone stopped before it reported its lines"
    return redact(text, _saved().values())[:160]


def _samples(audio):
    import numpy as np
    if type(audio).__module__.startswith("mlx"):
        import mlx.core as mx
        audio = audio.astype(mx.float32)  # numpy has no bfloat16
    return np.asarray(audio, dtype=np.float32).reshape(-1)


class KokoroPlayer:
    """Speaks a sample through this Mac's speakers with the downloaded Kokoro model (loaded on first use)."""

    def __init__(self, load=None, run=subprocess.run):
        self._load, self._run = load, run
        self._model = None

    def play(self, voice: str, speed: float, text: str) -> None:
        import numpy as np
        snapshot = models.local_path(models.TTS)
        if snapshot is None:
            raise ActionError("could not play the sample: the speech model is not downloaded")
        path = voices.voice_path(voice)
        if path is None:  # a bare voice name would make mlx-audio fetch it from another repo
            raise ActionError(f"could not play the sample: the voice {voice} is not installed")
        if self._model is None:
            load = self._load
            if load is None:
                from mlx_audio.tts.utils import load_model as load
            self._model = load(str(snapshot))
        parts = [_samples(r.audio) for r in self._model.generate(text=text, voice=str(path), speed=speed,
                                                                  lang_code=voices.lang_code(voice))]
        pcm = (np.clip(np.concatenate(parts), -1.0, 1.0) * 32767).astype("<i2")
        fd, wav = tempfile.mkstemp(prefix="ai-phone-voice-", suffix=".wav")
        os.close(fd)
        try:
            with wave.open(wav, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm.tobytes())
            self._run(["afplay", wav], timeout=30, check=True, capture_output=True)
        finally:
            os.unlink(wav)
