import asyncio
import re

import pytest

from ai_phone.calls import CallRegistry, ListenerBusy


def test_ids_are_six_hex_and_distinct_among_the_records_held():
    reg = CallRegistry()
    made = []
    for _ in range(200):
        rec = reg.new("agent", "out", "main", "+15550000000")
        made.append(rec)
        assert re.fullmatch(r"[0-9a-f]{6}", rec.id)
        held = made[-(reg.retain + 1):]        # the call in progress and the last 20 that ended
        assert all(reg.get(r.id) is r for r in held) and len({r.id for r in held}) == len(held)
        rec.finish("completed")
        reg.end_current(rec)
        held = made[-reg.retain:]              # the last 20 that ended
        assert all(reg.get(r.id) is r for r in held) and len({r.id for r in held}) == len(held)


def test_a_new_id_is_drawn_again_while_a_held_record_has_it(monkeypatch):
    from ai_phone import calls
    drawn = iter(["aaaaaa", "aaaaaa", "bbbbbb"])
    sizes = []

    def fake_token_hex(nbytes):
        sizes.append(nbytes)
        return next(drawn)

    monkeypatch.setattr(calls.secrets, "token_hex", fake_token_hex)
    reg = CallRegistry()
    first = reg.new("agent", "out", "main", "x")
    first.finish("completed")
    reg.end_current(first)                     # ended, but still held
    second = reg.new("relay", "out", "main", "y")
    assert first.id == "aaaaaa" and second.id == "bbbbbb" and sizes == [3, 3, 3]
    assert reg.get("aaaaaa") is first and reg.get("bbbbbb") is second


def test_registry_keeps_the_current_call_and_the_last_twenty_ended():
    reg = CallRegistry()
    made = []
    for i in range(25):
        rec = reg.new("agent", "out", "main", f"+1555000{i:04d}")
        rec.finish("completed")
        reg.end_current(rec)
        made.append(rec)
    live = reg.new("relay", "in", "main", "+15559999999")
    assert reg.current is live and reg.get(live.id) is live
    assert [reg.get(r.id) is r for r in made] == [False] * 5 + [True] * 20


def test_call_id_pattern_is_anchored():
    from ai_phone.control.protocol import CALL_ID_RE
    assert CALL_ID_RE.match("a1b2c3") and CALL_ID_RE.search("a1b2c3")
    for bad in ("a1b2c3x", "xa1b2c3", "a1b2c3\n", "A1B2C3", "a1b2c"):
        assert not CALL_ID_RE.match(bad) and not CALL_ID_RE.search(bad)


def test_result_shape_and_transcript():
    reg = CallRegistry()
    rec = reg.new("agent", "out", "main", "+15550000000", prompt="Ask the hours.")
    rec.answered = rec.created + 1
    rec.add_line("agent", "Hi, this is an AI assistant calling.")
    rec.add_line("them", "  Hello?  ")
    rec.add_line("agent", "   ")  # blank lines are not recorded
    rec.finish("completed", ended_by="them")
    rec.ended = rec.created + 4.5
    r = rec.result()
    assert r["call_id"] == rec.id and r["mode"] == "agent" and r["outcome"] == "completed" and r["ended_by"] == "them"
    assert [(t["who"], t["text"]) for t in r["transcript"]] == [("agent", "Hi, this is an AI assistant calling."), ("them", "Hello?")]
    assert r["answered_at"] == rec.created + 1 and r["ended_at"] == rec.created + 4.5 and r["duration_sec"] == 3.5
    assert "transcript" not in rec.summary() and rec.summary()["duration_sec"] == 3.5
    unanswered = reg.new("agent", "out", "main", "+15550000001")
    unanswered.finish("no_answer")
    assert unanswered.result()["duration_sec"] is None


def test_finish_keeps_an_ended_by_set_earlier_and_only_happens_once():
    rec = CallRegistry().new("relay", "out", "main", "x")
    rec.ended_by = "hangup_command"
    rec.finish("completed", ended_by="them")
    rec.finish("busy", ended_by="error")
    assert rec.outcome == "completed" and rec.ended_by == "hangup_command"


@pytest.mark.asyncio
async def test_heard_is_merged_until_read_then_ended_is_repeatable():
    rec = CallRegistry().new("relay", "out", "main", "x")
    rec.heard("Sure.")
    rec.heard("What name is it under?")
    assert await rec.next_event(0.1) == {"kind": "heard", "text": "Sure. What name is it under?"}
    assert await rec.next_event(0.05) is None
    rec.heard("Hello?")
    rec.finish("completed", ended_by="them")
    assert (await rec.next_event(0.1))["kind"] == "heard"
    first = await rec.next_event(0.1)
    assert first["kind"] == "ended" and first["result"]["ended_by"] == "them"
    assert (await rec.next_event(0.1))["kind"] == "ended"


@pytest.mark.asyncio
async def test_next_event_wakes_on_new_speech():
    rec = CallRegistry().new("relay", "out", "main", "x")
    waiter = asyncio.create_task(rec.next_event(2))
    await asyncio.sleep(0.05)
    rec.heard("Yes.")
    assert await asyncio.wait_for(waiter, 1) == {"kind": "heard", "text": "Yes."}


@pytest.mark.asyncio
async def test_a_waiting_next_event_is_woken_by_the_end_of_the_call():
    rec = CallRegistry().new("relay", "out", "main", "x")
    waiter = asyncio.create_task(rec.next_event(5))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    rec.finish("completed", ended_by="them")
    event = await asyncio.wait_for(waiter, 1)
    assert event["kind"] == "ended" and event["result"]["ended_by"] == "them" and rec.end_reported


@pytest.mark.asyncio
async def test_waiting_for_connection_returns_when_the_audio_comes_up():
    rec = CallRegistry().new("relay", "out", "main", "x")
    waiter = asyncio.create_task(rec.wait_connected(5))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    rec.mark_connected()
    assert await asyncio.wait_for(waiter, 1) is True


@pytest.mark.asyncio
async def test_connected_and_ended_waits():
    rec = CallRegistry().new("relay", "out", "main", "x")
    assert await rec.wait_connected(0.05) is False
    rec.mark_connected()
    assert await rec.wait_connected(0.05) is True
    assert await rec.wait_ended(0.05) is False
    rec.finish("completed")
    assert await rec.wait_ended(0.05) is True


@pytest.mark.asyncio
async def test_waiting_for_connection_stops_when_the_call_ends():
    rec = CallRegistry().new("relay", "out", "main", "x")
    asyncio.get_running_loop().call_later(0.05, rec.finish, "busy")
    t0 = asyncio.get_running_loop().time()
    assert await rec.wait_connected(5) is False
    assert asyncio.get_running_loop().time() - t0 < 1


@pytest.mark.asyncio
async def test_unread_ended_is_the_latest_relay_call_nobody_heard_end():
    reg = CallRegistry()
    agent = reg.new("agent", "out", "main", "x")
    agent.finish("completed")
    reg.end_current(agent)
    assert reg.unread_ended() is None                  # phone agent results go to the call command
    old = reg.new("relay", "out", "main", "x")
    old.finish("completed")
    reg.end_current(old)
    new = reg.new("relay", "in", "main", "y")
    new.finish("completed")
    reg.end_current(new)
    assert reg.unread_ended() is new
    assert (await new.next_event(0))["kind"] == "ended" and new.end_reported
    assert reg.unread_ended() is old


@pytest.mark.asyncio
async def test_one_relay_listener_at_a_time_and_delivery():
    reg = CallRegistry()
    fut = reg.start_listening()
    assert reg.listening
    with pytest.raises(ListenerBusy):
        reg.start_listening()
    rec = reg.new("relay", "in", "main", "+15550000000")
    assert reg.deliver_incoming(rec) is True
    assert await fut is rec and not reg.listening
    assert reg.deliver_incoming(rec) is False


@pytest.mark.asyncio
async def test_stop_listening_frees_the_slot():
    reg = CallRegistry()
    fut = reg.start_listening()
    reg.stop_listening(fut)
    assert not reg.listening and fut.cancelled()
    reg.start_listening()


@pytest.mark.asyncio
async def test_a_cancelled_listener_frees_the_slot_by_itself():
    reg = CallRegistry()

    async def listen():
        return await reg.start_listening()

    task = asyncio.create_task(listen())
    await asyncio.sleep(0)                     # the task is now waiting on its listener future
    assert reg.listening
    task.cancel()                              # the client went away; nobody calls stop_listening
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not reg.listening
    assert reg.deliver_incoming(reg.new("relay", "in", "main", "+15550000000")) is False
    reg.start_listening()                      # no ListenerBusy


def test_by_pj_matches_only_the_live_current_call():
    reg = CallRegistry()
    rec = reg.new("agent", "in", "main", "x")
    rec.pj_call_id = 4
    assert reg.by_pj(4) is rec and reg.by_pj(5) is None
    rec.finish("completed")
    assert reg.by_pj(4) is None
