"""Supervisor: spawns the three model workers, starts PJSIP with one account per line, serves the control socket,
and runs one TurnController per call (inbound or outbound)."""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import socket
import time
from pathlib import Path

from ai_phone import paths
from ai_phone.config import Config, LineConfig, line_password_env
from ai_phone.control.protocol import EXIT
from ai_phone.control.server import ControlServer
from ai_phone.pipeline.timing import TurnLog
from ai_phone.pipeline.turn import TurnController
from ai_phone.pipeline.vad import Endpointer, EnergyVad, SileroVad
from ai_phone.secrets import load_secrets, redact
from ai_phone.services.client import WorkerClient, is_secret_env
from ai_phone.services.ipc import STT_IN_FRAME, TTS_OUT_FRAME, untag_frame
from ai_phone.telephony import events as ev
from ai_phone.telephony.endpoint import BusyError, DialError, LineSpec, Telephony
from ai_phone.telephony.rings import FrameRing
from ai_phone.telephony.state import LineStatus, StateFile, next_state

log = logging.getLogger("ai-phone")
FACTORIES = {
    "real": {"stt": "ai_phone.services.backends.mlx_parakeet:ParakeetSTT",
             "llm": {"t9": "ai_phone.services.backends.mlx_lm_backend:make_t9", "t4": "ai_phone.services.backends.mlx_lm_backend:make_t4"},
             "tts": "ai_phone.services.backends.mlx_kokoro:KokoroTTS"},
    "fake": {"stt": "ai_phone.services.backends.fake:FakeSTT",
             "llm": {"t9": "ai_phone.services.backends.fake:FakeLLM", "t4": "ai_phone.services.backends.fake:FakeLLM"},
             "tts": "ai_phone.services.backends.fake:FakeTTS"},
}
APOLOGY = "Sorry, I lost my train of thought. Could you say that again?"
GOODBYE = "I'm having trouble thinking right now. Please call back later. Goodbye."


class Daemon:
    def __init__(self, cfg: Config, tier: str, log_dir: Path, lan: bool, fake: bool = False):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        self.cfg, self.tier, self.log_dir, self.fake, self.lan = cfg, tier, Path(log_dir).expanduser(), fake, lan
        self.secrets = load_secrets(paths.secrets_path())
        self.remote_brain = cfg.brain.kind == "openai_compatible"
        self._take_secrets_from_environment()
        self._drop_secret_environment()
        self.line_status: dict[str, LineStatus] = {}
        self.state_file = StateFile(paths.state_path())
        self.active_call: dict | None = None
        self._call_waiters: list[asyncio.Queue] = []
        self._outbound: dict | None = None   # {"call_id", "persona", "send"} while a control-socket call is in flight
        self._bg_tasks: set[asyncio.Task] = set()  # fire-and-forget tasks the loop only holds weakly

    def _secret_env_names(self) -> list[str]:
        names = [lc.password_env or line_password_env(lid) for lid, lc in self.cfg.lines.items() if lc.kind == "trunk"]
        if self.cfg.brain.api_key_env:
            names.append(self.cfg.brain.api_key_env)
        return names

    def _take_secrets_from_environment(self) -> None:
        """Secrets the user exported in the shell move from os.environ into self.secrets before any worker is
        spawned, so no child process inherits them (spawned workers copy the daemon's environment)."""
        for name in self._secret_env_names():
            value = os.environ.pop(name, None)
            if value and name not in self.secrets:
                self.secrets[name] = value

    def _drop_secret_environment(self) -> None:
        """Every other secret-looking variable the shell exported (unrelated API keys, tokens) leaves the daemon's
        environment for good. The daemon needs none of them, and every child it will ever spawn inherits what is
        left: the model workers, and the multiprocessing resource tracker that appears with the first shared ring,
        before any worker's own scrub runs (seen on the gate: the tracker held a shell's ELEVENLABS_API_KEY)."""
        for name in [n for n in os.environ if is_secret_env(n)]:
            del os.environ[name]

    # ---- system prompt and cached phrases -------------------------------------------------------
    def system_prompt_for(self, text: str) -> str:
        # a remote brain gets no host metadata: nothing about this machine leaves it
        return text if self.remote_brain else f"{text} You are running on {socket.gethostname()} ({platform.machine()})."

    async def _cache_phrase(self, tts: WorkerClient, ring: FrameRing, text: str) -> list[bytes]:
        gen = tts.next_gen()
        tts.send({"op": "tts.synth", "gen": gen, "seq": 0, "text": text})
        while True:
            m = await asyncio.wait_for(tts.events.get(), 120)
            if m["ev"] == "error":
                raise RuntimeError(f"TTS worker failed while caching a phrase: {m.get('message', '')[:200]}")
            if m["ev"] == "done" and m["gen"] == gen:
                break
        frames = []
        while (f := ring.pop()) is not None:
            g, payload = untag_frame(f)
            if g == (gen & 0xFFFFFFFF):
                frames.append(payload)
        return frames

    # ---- lines -----------------------------------------------------------------------------------
    def _line_specs(self) -> list[LineSpec]:
        lines = dict(self.cfg.lines)
        if not lines:
            lines["local"] = LineConfig(kind="sip", user="ai", aor="sip:ai@127.0.0.1", display_name="ai", accept_invites_from="lan", caller_id="sip:ai@127.0.0.1")
        specs = []
        for lid, lc in lines.items():
            if self.lan:
                lc.accept_invites_from = "lan"
            password = None
            if lc.kind == "trunk":
                env_name = lc.password_env or line_password_env(lid)
                password = self.secrets.get(env_name)
                if not password:
                    log.error("line %s skipped: password variable %s is not set (secrets.env or the environment)", lid, env_name)
                    self.line_status[lid] = LineStatus(lid, lc.kind, lc.number or "", "skipped", lc.registrar, None, f"{env_name} not set", time.time())
                    continue
            specs.append(LineSpec.from_config(lid, lc, password))
            self.line_status[lid] = LineStatus(lid, lc.kind, lc.number or lc.aor or "", "registering" if lc.kind == "trunk" else "unregistered",
                                               lc.registrar, None, None, time.time())
        return specs

    def _write_state(self) -> None:
        brain = {"kind": self.cfg.brain.kind, "base_url": self.cfg.brain.base_url if self.remote_brain else None}
        self.state_file.write(self.line_status, self.active_call, brain)

    def _on_reg(self, e: ev.RegState) -> None:
        st = self.line_status.get(e.line_id)
        if st is None:
            return
        new = next_state(st.state, e.active, e.code)
        if new != st.state:
            st.since = time.time()
        st.state, st.last_error = new, (None if e.active else f"{e.code} {e.reason}")
        st.expires_at = time.time() + e.expires_sec if e.active else None
        log.info("registration line=%s state=%s code=%s reason=%s", e.line_id, new, e.code, e.reason)
        self._write_state()

    # ---- main --------------------------------------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()
        rx, tx = FrameRing(None, 100), FrameRing(None, 400)
        stt_in, tts_out = FrameRing(None, 400, frame_bytes=STT_IN_FRAME), FrameRing(None, 400, frame_bytes=TTS_OUT_FRAME)
        F = FACTORIES["fake" if self.fake else "real"]
        default_prompt = self.system_prompt_for(self.cfg.inbound.instructions)
        wa = {} if self.fake else {"wire_memory": self.cfg.models.wire_memory}  # the MLX backends pin their weights
        kw = self.cfg.models.keep_warm_sec
        stt = WorkerClient("stt", F["stt"], loop, stt_in, None, dict(wa), keep_warm_sec=kw)
        if self.remote_brain:
            b = self.cfg.brain
            key = self.secrets.get(b.api_key_env) if b.api_key_env else None
            host = b.base_url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
            if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
                log.warning("Brain: %s. Transcripts of what callers say, the system prompt and the conversation history are sent there "
                            "(not audio). What that service keeps is up to that service.", b.base_url)
            llm = WorkerClient("llm", "ai_phone.services.backends.openai_compat:make_openai", loop, None, None, {"system_prompt": default_prompt},
                               factory_args={"base_url": b.base_url, "model": b.model, "api_key": key, "connect_timeout_ms": b.connect_timeout_ms,
                                             "first_token_timeout_ms": b.first_token_timeout_ms, "stream_idle_timeout_ms": b.stream_idle_timeout_ms,
                                             "max_response_chars": b.max_response_chars})
        else:
            llm = WorkerClient("llm", F["llm"][self.tier], loop, None, None, {"system_prompt": default_prompt, **wa}, keep_warm_sec=kw)
        tts = WorkerClient("tts", F["tts"], loop, None, tts_out, dict(wa), keep_warm_sec=kw)
        tel: Telephony | None = None
        control: ControlServer | None = None
        started: list[WorkerClient] = []
        try:  # from here on every failure tears down what exists: workers, rings, endpoint, socket
            warm = []
            for client in (stt, llm, tts):  # one at a time, so a failure part-way stops exactly the workers that exist
                try:
                    warm.append(await client.start())
                except RuntimeError as e:
                    log.error("%s", redact(str(e), self.secrets.values()))
                    raise SystemExit(1)
                started.append(client)
            cached = {"prompt": await self._cache_phrase(tts, tts_out, "Still there?"),
                      "warning": await self._cache_phrase(tts, tts_out, "This call will end in thirty seconds."),
                      "apology": await self._cache_phrase(tts, tts_out, APOLOGY),
                      "goodbye": await self._cache_phrase(tts, tts_out, GOODBYE)}
            greetings = {"default": await self._cache_phrase(tts, tts_out, self.cfg.inbound.greeting)}
            openings = {"default": await self._cache_phrase(tts, tts_out, "Hi, this is an AI assistant calling.")}
            specs = self._line_specs()
            tel = Telephony(self.cfg, specs, loop, events, rx, tx)
            tel.start()
            for w in tel.peer_warnings:
                log.warning(w)
            self._write_state()
            turn_log = TurnLog(self.log_dir / "turns.jsonl")
            stop = asyncio.Event()
            for s in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(s, stop.set)
            ctx = dict(tel=tel, stt=stt, llm=llm, tts=tts, rx=rx, tx=tx, stt_in=stt_in, tts_out=tts_out, cached=cached,
                       greetings=greetings, openings=openings, turn_log=turn_log, events=events, stop=stop, specs={s.id: s for s in specs})
            control = ControlServer(paths.socket_path(), lambda msg, send: self._control(msg, send, ctx))
            await control.start()
            log.info("ready lines=%s brain=%s tier=%s warm=%s", [s.id for s in specs], self.cfg.brain.kind, self.tier, warm)
            print("ready", flush=True)
            while not stop.is_set():
                try:
                    e = await asyncio.wait_for(events.get(), 0.2)
                except asyncio.TimeoutError:
                    continue
                if isinstance(e, ev.IncomingCall):
                    log.info("incoming on %s from %s (%s)", e.line_id, e.from_uri, e.source_ip)
                    tel.answer(e.call_id)
                    self.active_call = {"call_id": e.call_id, "line_id": e.line_id, "remote": e.from_uri, "started_at": time.time(), "direction": "in"}
                    self._write_state()
                elif isinstance(e, ev.CallRejected):
                    log.info("rejected on %s: %s with %s", e.line_id, e.source_ip, e.code)
                elif isinstance(e, ev.MediaActive):
                    lid = e.line_id  # the event is authoritative; current_call is mutable state
                    ob = self._outbound if self._outbound and self._outbound["call_id"] == e.call_id else None
                    if ob is None:
                        await self._run_call(e.call_id, lid, "in", None, ctx)
                    else:  # the control handler is waiting on its queue; the main loop is the only consumer of `events`
                        send = ob["send"]
                        await self._run_call(e.call_id, lid, "out", ob["persona"], ctx,
                                             on_turn_event=lambda rec: self._send_quiet(send, {"event": "turn", **{k: rec[k] for k in ("turn", "first_audible_ms", "bargein")}}))
                        for q in self._call_waiters:
                            q.put_nowait(("call_done", e.call_id))
                        self._outbound = None
                elif isinstance(e, ev.RegState):
                    self._on_reg(e)
                elif isinstance(e, ev.OutboundProgress):
                    log.info("outbound line=%s call=%s state=%s code=%s %s", e.line_id, e.call_id, e.state, e.code, e.reason)
                    for q in self._call_waiters:
                        q.put_nowait(e)
                elif isinstance(e, ev.CallStateChanged) and e.state == "ended":
                    if self.active_call is not None and self.active_call.get("call_id") == e.call_id:
                        # the active call ended before media became active (caller hung up while ringing, busy, rejected…);
                        # a rejected second caller's own "ended" carries a different id and must not touch it
                        self.active_call = None
                        self._write_state()
                    # an `ended` seen here was not consumed by a controller (the controller runs inside this loop and eats its own),
                    # so nothing else will release the claim; release_call is a no-op for any id that is not the claimed call
                    tel.release_call(e.call_id)
                    for q in self._call_waiters:
                        q.put_nowait(e)
        finally:
            if control is not None:
                await control.stop()
            if tel is not None:
                tel.stop()
            if started:
                await asyncio.gather(*(c.stop() for c in started))
            for r in (rx, tx, stt_in, tts_out):
                r.close()
                r.unlink()

    # ---- control socket -------------------------------------------------------------------------------
    async def _control(self, msg: dict, send, ctx: dict) -> None:
        tel = ctx["tel"]
        if msg["cmd"] == "status":
            await send({"ok": True, "lines": {k: v.__dict__ for k, v in self.line_status.items()}, "active_call": self.active_call})
            return
        if msg["cmd"] == "hangup":
            cid = tel.current_call_id
            if cid is not None:
                tel.hangup(cid)
            await send({"ok": True, "hung_up": cid is not None})
            return
        # call
        line_id = msg.get("line") or self._default_line_for(msg.get("to", ""), ctx["specs"], tel)
        if line_id not in ctx["specs"]:
            await send({"ok": False, "error": f"no such line {line_id!r}" if msg.get("line") else "no line can dial that target (no registered trunk for a phone number, or no sip line for a sip: address)"})
            return
        persona = "default"
        q: asyncio.Queue = asyncio.Queue()
        self._call_waiters.append(q)
        cid: int | None = None   # set once PJSIP has a call; cleanup below must not assume it
        outcome = "rejected"
        detail = ""
        try:
            try:
                cid = tel.make_call(line_id, msg["to"], float(msg.get("timeout_sec", 30)))
            except BusyError:
                await send({"ok": False, "error": "busy"})
                return
            except DialError as e:
                await send({"event": "ended", "reason": "rejected", "detail": str(e), "exit_code": EXIT["rejected"]})
                return  # nothing was claimed (make_call cleared it) and no state was written
            log.info("control: call on line %s to %s -> call_id %s", line_id, msg["to"], cid)
            self._outbound = {"call_id": cid, "persona": persona, "send": send}
            self.active_call = {"call_id": cid, "line_id": line_id, "remote": msg["to"], "started_at": time.time(), "direction": "out"}
            self._write_state()
            while True:  # progress arrives via the main loop, which is the only consumer of the telephony queue
                e = await q.get()
                if isinstance(e, ev.OutboundProgress) and (e.call_id == cid or (e.call_id == -1 and e.line_id == line_id)):  # -1: "dialling", before PJSIP assigns the id
                    await self._send_quiet(send, {"event": e.state, "code": e.code, "uri": e.reason if e.state == "dialling" else "",
                                                  "reason": "" if e.state == "dialling" else e.reason})
                    if e.state in ("busy", "no_answer", "rejected", "redirected"):
                        outcome = e.state
                        detail = f"{e.code} {e.reason}".strip()
                        break
                    if e.state == "answered":
                        outcome = "answered"
                        continue  # keep waiting: the main loop runs the conversation and posts call_done
                elif isinstance(e, tuple) and e[0] == "call_done" and e[1] == cid:
                    outcome = "answered"  # the conversation ran, whatever order PJSIP delivered media and CONFIRMED in
                    break
                elif isinstance(e, ev.CallStateChanged) and e.call_id == cid and e.state == "ended" and self._outbound is not None:
                    break  # ended before media became active
            if outcome == "rejected" and detail and detail.split(" ", 1)[0] in ("403", "603"):
                if not any(w in detail.lower() for w in ("whitelist", "account level", "verified")):  # the carrier said why already
                    detail += (" (the carrier refused the call: either the caller ID is not one of the line's numbers or a verified one,"
                               " or the destination is not allowed for this account or its outbound voice profile)")
            await self._send_quiet(send, {"event": "ended", "reason": outcome, "detail": detail, "exit_code": EXIT.get(outcome, EXIT["rejected"])})
        finally:
            self._outbound = None
            self._call_waiters.remove(q)
            if cid is not None and outcome != "answered":
                if self.active_call is not None and self.active_call.get("call_id") == cid:
                    self.active_call = None  # never reached media, so _run_call never cleared it
                    self._write_state()
                # the main loop releases on `ended` too; both are idempotent, so the claim can never be left behind
                if getattr(tel, "current_call", None) is not None and tel.current_call.getId() == cid and tel.current_call.ended:
                    tel.release_call(cid)

    @staticmethod
    def _default_line_for(target: str, specs: dict, tel) -> str | None:
        """No --line given: a sip: address goes out on the first sip line, or nowhere;
        a phone number goes out on the first registered trunk line that has a dial domain."""
        if target.startswith("sip:"):
            sip_lines = [lid for lid, s in specs.items() if s.kind == "sip"]
            return sip_lines[0] if sip_lines else None  # never a trunk by accident: say --line if that is really wanted
        for lid, s in specs.items():
            acc = tel.accounts.get(lid) if hasattr(tel, "accounts") else None
            if s.kind == "trunk" and s.dial_domain and (acc is None or acc.registered):
                return lid
        return None

    async def _send_quiet(self, send, msg: dict) -> None:
        try:
            await send(msg)
        except ConnectionError:
            pass  # the CLI went away; the call continues

    # ---- one call --------------------------------------------------------------------------------------
    async def _run_call(self, call_id, line_id, direction, persona_name, ctx, on_turn_event=None):
        tel, stt, llm, tts = ctx["tel"], ctx["stt"], ctx["llm"], ctx["tts"]
        rx, tx, stt_in, tts_out, cached = ctx["rx"], ctx["tx"], ctx["stt_in"], ctx["tts_out"], ctx["cached"]
        persona_name = "default"
        vad = EnergyVad() if self.fake else SileroVad()
        n = [0]
        turn_log, events, stop = ctx["turn_log"], ctx["events"], ctx["stop"]

        def on_turn(t):
            rec = t.to_record(str(call_id), n[0])
            rec["line_id"], rec["persona"] = line_id, persona_name
            turn_log.write(rec)
            n[0] += 1
            log.info("turn %s line=%s first_audible=%s bargein=%s brain_error=%s", n[0], line_id, t.first_audible_ms(), t.bargein, t.brain_error)
            if on_turn_event is not None:
                task = asyncio.get_running_loop().create_task(on_turn_event(rec))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

        tc = TurnController(self.cfg.conversation, tel.port, stt, llm, tts, rx, tx, stt_in, tts_out,
                            Endpointer(vad, self.cfg.conversation.endpoint_silence_ms), ctx["greetings"][persona_name],
                            self.system_prompt_for(self.cfg.inbound.instructions), on_turn, prompt_frames=cached["prompt"], warning_frames=cached["warning"],
                            rtp_last_rx_ns=lambda: tel.port.last_rx_ns, apology_frames=cached["apology"], goodbye_frames=cached["goodbye"],
                            opening_frames=ctx["openings"][persona_name] if direction == "out" else None, direction=direction)
        call_stop = asyncio.Event()
        runner = asyncio.create_task(tc.run(call_stop))
        while not call_stop.is_set() and not stop.is_set():
            if tc.hangup_requested.is_set():
                log.info("hangup: %s", tc.end_reason)
                tel.hangup(call_id)
                break
            try:
                e = await asyncio.wait_for(events.get(), 0.1)
            except asyncio.TimeoutError:
                continue
            if isinstance(e, ev.CallStateChanged) and e.state == "ended":
                if e.call_id == call_id:
                    break
                log.info("ignoring end of unrelated call %s while call %s is active", e.call_id, call_id)
            elif isinstance(e, ev.CallRejected):
                log.info("rejected on %s: %s with %s", e.line_id, e.source_ip, e.code)
            elif isinstance(e, ev.OutboundProgress):
                # PJSIP can report media active before CONFIRMED, so "answered" may arrive while the call already runs
                log.info("outbound line=%s call=%s state=%s code=%s %s", e.line_id, e.call_id, e.state, e.code, e.reason)
                for q in self._call_waiters:
                    q.put_nowait(e)
            elif isinstance(e, ev.RegState):
                self._on_reg(e)   # registration changes keep flowing during a call
        call_stop.set()
        await runner
        for c in (stt, llm, tts):
            c.cancel(c.current_gen)
        if tc.timer is not None and tc.timer.t0 is not None:
            rec = tc.timer.to_record(str(call_id), n[0])
            rec.update(incomplete=True, state_at_end=tc.state, line_id=line_id, persona=persona_name)
            turn_log.write(rec)
        port = tel.port
        if port is not None and hasattr(port, "cancel_flush"):
            port.cancel_flush()  # a flush the PJSIP thread never performed must not hit the next call's greeting
        for r in (rx, tx, tts_out):
            r.flush()
        self.active_call = None
        self._write_state()
        tel.release_call(call_id)  # only now may the next call be claimed
