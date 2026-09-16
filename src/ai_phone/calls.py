"""Call records: one per call the daemon handles, kept in memory (the call in progress and the last 20 that ended).
A record outlives the command that started it, which is what lets `call --relay` return while the call goes on."""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

RETAIN = 20


class ListenerBusy(RuntimeError):
    """Another `listen` is already waiting for incoming calls."""


@dataclass
class TranscriptLine:
    who: str          # "agent" | "them"
    text: str
    at: float
    interrupted: bool = False


@dataclass(eq=False)
class CallRecord:
    id: str
    mode: str                     # "agent" | "relay"
    direction: str                # "in" | "out"
    line_id: str
    remote: str
    prompt: str | None = None
    pj_call_id: int | None = None
    created: float = field(default_factory=time.time)
    answered: float | None = None
    ended: float | None = None
    outcome: str | None = None    # completed | busy | no_answer | rejected | redirected | cancelled
    detail: str = ""
    ended_by: str | None = None   # agent | them | timeout | silence | max_duration | error | hangup_command
    transcript: list[TranscriptLine] = field(default_factory=list)
    end_reported: bool = False    # an `ended` event or a hangup result carrying this call's end has been handed out
    _unread: list[dict] = field(default_factory=list, repr=False)
    _changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _connected: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _ended_evt: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    # ---- transcript and relay events ------------------------------------------------------------
    def add_line(self, who: str, text: str, interrupted: bool = False) -> None:
        text = (text or "").strip()
        if text:
            self.transcript.append(TranscriptLine(who, text, time.time(), interrupted))

    def heard(self, text: str) -> None:
        """Relay mode: the other person finished speaking. Unread speech is merged so `listen` returns it together.
        (The transcript line is written by the controller, not here.)"""
        text = (text or "").strip()
        if not text:
            return
        if self._unread and self._unread[-1]["kind"] == "heard":
            self._unread[-1]["text"] += " " + text
        else:
            self._unread.append({"kind": "heard", "text": text})
        self._changed.set()

    async def next_event(self, timeout: float) -> dict | None:
        """The next unread event: merged `heard` text first, then `ended` (repeatable once the call is over).
        None when nothing happens within `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while True:
            if self._unread:
                return self._unread.pop(0)
            if self.is_ended:
                self.end_reported = True
                return {"kind": "ended", "result": self.result()}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except asyncio.TimeoutError:
                return None

    def put_back(self, event: dict) -> None:
        """A listen cancelled before its reply went out returns the event, so the next listen gets it."""
        if event.get("kind") == "heard":
            if self._unread and self._unread[0]["kind"] == "heard":
                self._unread[0]["text"] = event["text"] + " " + self._unread[0]["text"]
            else:
                self._unread.insert(0, dict(event))
            self._changed.set()
        elif event.get("kind") == "ended":
            self.end_reported = False

    # ---- lifecycle ----------------------------------------------------------------------------------
    def mark_connected(self) -> None:
        self._connected.set()

    async def wait_connected(self, timeout: float) -> bool:
        """True once the call's audio is up; False on timeout, or as soon as the call ends without connecting."""
        if self._connected.is_set():
            return True
        waiters = [asyncio.ensure_future(self._connected.wait()), asyncio.ensure_future(self._ended_evt.wait())]
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        return self._connected.is_set()

    def finish(self, outcome: str, ended_by: str | None = None, detail: str = "") -> None:
        if self.ended is not None:
            return
        self.ended = time.time()
        self.outcome = outcome
        self.ended_by = self.ended_by or ended_by
        self.detail = detail
        self._changed.set()
        self._ended_evt.set()

    @property
    def is_ended(self) -> bool:
        return self.ended is not None

    async def wait_ended(self, timeout: float) -> bool:
        return await _wait(self._ended_evt, timeout)

    # ---- views ----------------------------------------------------------------------------------------
    def result(self) -> dict:
        duration = None if self.answered is None else round((self.ended or time.time()) - self.answered, 1)
        return {"call_id": self.id, "mode": self.mode, "direction": self.direction, "line": self.line_id,
                "remote": self.remote, "outcome": self.outcome, "detail": self.detail, "ended_by": self.ended_by,
                "created_at": self.created, "answered_at": self.answered, "ended_at": self.ended,
                "duration_sec": duration,
                "transcript": [{"who": t.who, "text": t.text, "at": t.at, "interrupted": t.interrupted}
                               for t in self.transcript]}

    def summary(self) -> dict:
        r = self.result()
        r.pop("transcript")
        return r


async def _wait(evt: asyncio.Event, timeout: float) -> bool:
    if evt.is_set():
        return True
    try:
        await asyncio.wait_for(evt.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


class CallRegistry:
    def __init__(self, retain: int = RETAIN):
        self.retain = retain
        self.current: CallRecord | None = None
        self._records: dict[str, CallRecord] = {}
        self._listener: asyncio.Future | None = None

    def new(self, mode: str, direction: str, line_id: str, remote: str, prompt: str | None = None) -> CallRecord:
        cid = secrets.token_hex(3)
        while cid in self._records:
            cid = secrets.token_hex(3)
        rec = CallRecord(cid, mode, direction, line_id, remote, prompt)
        self._records[cid] = rec
        self.current = rec
        self._prune()
        return rec

    def get(self, call_id: str) -> CallRecord | None:
        return self._records.get(call_id)

    def by_pj(self, pj_call_id: int) -> CallRecord | None:
        c = self.current
        return c if c is not None and c.pj_call_id == pj_call_id and not c.is_ended else None

    def end_current(self, rec: CallRecord) -> None:
        if self.current is rec:
            self.current = None
        self._prune()

    def unread_ended(self) -> CallRecord | None:
        """The most recently created relay call that has ended and whose end nobody has been told about yet.
        Creation order, not end time: two calls can end within the same clock tick."""
        for r in reversed(self._records.values()):
            if r.mode == "relay" and r.is_ended and not r.end_reported:
                return r
        return None

    def _prune(self) -> None:
        ended = sorted((r for r in self._records.values() if r.is_ended and r is not self.current), key=lambda r: r.ended)
        for r in ended[:max(0, len(ended) - self.retain)]:
            del self._records[r.id]

    # ---- the relay listener: one waiter for the next incoming call ------------------------------------
    @property
    def listening(self) -> bool:
        return self._listener is not None and not self._listener.done()

    def start_listening(self) -> asyncio.Future:
        if self.listening:
            raise ListenerBusy("another listener is already waiting for calls")
        self._listener = asyncio.get_running_loop().create_future()
        return self._listener

    def stop_listening(self, fut: asyncio.Future) -> None:
        if not fut.done():
            fut.cancel()
        if self._listener is fut:
            self._listener = None

    def deliver_incoming(self, rec: CallRecord) -> bool:
        if not self.listening:
            return False
        self._listener.set_result(rec)
        self._listener = None
        return True
