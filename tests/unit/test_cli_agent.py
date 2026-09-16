import json
from pathlib import Path

import pytest

from ai_phone.cli import main
from tests.unit.test_cli_call import _serve, sock_dir  # noqa: F401  (fixture)

RESULT = {"call_id": "c7f3a2", "mode": "agent", "direction": "out", "line": "main", "remote": "+15550000000",
          "outcome": "completed", "detail": "", "ended_by": "agent", "created_at": 1.0, "answered_at": 2.0,
          "ended_at": 104.0, "duration_sec": 102.0,
          "transcript": [{"who": "agent", "text": "Hi, this is an AI assistant calling.", "at": 2.0, "interrupted": False},
                         {"who": "them", "text": "Hello?", "at": 3.0, "interrupted": False}]}


def run_with(sock_dir, monkeypatch, handler, argv):
    monkeypatch.setenv("AI_PHONE_HOME", str(sock_dir))
    stop, t = _serve(sock_dir / "control.sock", handler)
    try:
        return main(argv)
    finally:
        stop.set(); t.join(2)


def test_agent_call_prints_the_transcript(sock_dir, monkeypatch, capsys):
    seen = {}

    async def handler(msg, send):
        seen.update(msg)
        await send({"event": "dialling", "uri": "sip:+15550000000@x"})
        await send({"event": "answered"})
        await send({"event": "ended", "reason": "completed", "detail": "", "exit_code": 0, "call_id": "c7f3a2", "result": RESULT})

    rc = run_with(sock_dir, monkeypatch, handler, ["call", "+15550000000", "--prompt", "Ask the hours.", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0 and seen["prompt"] == "Ask the hours." and seen["relay"] is False
    assert "call c7f3a2 ended: completed, hung up by agent, 1m42s" in out
    assert "  agent: Hi, this is an AI assistant calling." in out and "  them:  Hello?" in out


def test_json_output_is_one_object_and_nothing_else(sock_dir, monkeypatch, capsys):
    async def handler(msg, send):
        await send({"event": "dialling", "uri": "x"})
        await send({"event": "ended", "reason": "completed", "detail": "", "exit_code": 0, "call_id": "c7f3a2", "result": RESULT})

    rc = run_with(sock_dir, monkeypatch, handler, ["call", "+15550000000", "--json", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0 and json.loads(out) == {"ok": True, **RESULT}


def test_relay_call_returns_at_answer(sock_dir, monkeypatch, capsys):
    async def handler(msg, send):
        assert msg["relay"] is True and msg["prompt"] is None
        await send({"event": "answered"})
        await send({"ok": True, "event": "answered", "call_id": "c7f3a2", "mode": "relay"})

    assert run_with(sock_dir, monkeypatch, handler, ["call", "+15550000000", "--relay", "--yes"]) == 0
    assert "call c7f3a2 answered" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["call", "+15550000000", "--relay", "--prompt", "x", "--yes"],
                                  ["call", "+15550000000", "--persona", "default", "--yes"],
                                  ["call", "+15550000000", "--prompt", "x" * 4001, "--yes"]])
def test_usage_errors_never_reach_the_daemon(sock_dir, monkeypatch, capsys, argv):
    async def handler(msg, send):
        raise AssertionError("the daemon must not be contacted")

    assert run_with(sock_dir, monkeypatch, handler, argv) == 2
    assert capsys.readouterr().out.startswith("error:")


def test_prompt_file(sock_dir, monkeypatch, tmp_path):
    p = tmp_path / "prompt.txt"
    p.write_text("Ask whether they deliver.\n")
    seen = {}

    async def handler(msg, send):
        seen.update(msg)
        await send({"event": "ended", "reason": "busy", "detail": "486", "exit_code": 5, "call_id": "c7f3a2", "result": None})

    assert run_with(sock_dir, monkeypatch, handler, ["call", "+15550000000", "--prompt-file", str(p), "--yes"]) == 5
    assert seen["prompt"] == "Ask whether they deliver."


@pytest.mark.parametrize("reply, text, code", [
    ({"ok": True, "kind": "heard", "text": "Sure, what name?"}, "heard: Sure, what name?", 0),
    ({"ok": True, "kind": "incoming", "call_id": "c9b1e0", "line": "main", "remote": "+15551234567"},
     "incoming call c9b1e0 on main from +15551234567", 0),
    ({"ok": True, "kind": "ended", "result": RESULT}, "call c7f3a2 ended: completed", 0),
    ({"ok": True, "kind": "timeout"}, "timeout", 10),
    ({"ok": False, "error": "another listener is already waiting for calls", "code": 12}, "error: another listener", 12),
])
def test_listen(sock_dir, monkeypatch, capsys, reply, text, code):
    async def handler(msg, send):
        assert msg == {"cmd": "listen", "call_id": None, "timeout_sec": 60.0}
        await send(reply)

    assert run_with(sock_dir, monkeypatch, handler, ["listen"]) == code
    assert text in capsys.readouterr().out


def test_say_and_its_json(sock_dir, monkeypatch, capsys):
    async def handler(msg, send):
        assert msg == {"cmd": "say", "call_id": "c7f3a2", "text": "Sam. Thanks."}
        await send({"ok": True, "result": "interrupted", "call_id": "c7f3a2"})

    assert run_with(sock_dir, monkeypatch, handler, ["say", "--call", "c7f3a2", "--json", "Sam.", "Thanks."]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "result": "interrupted", "call_id": "c7f3a2"}


def test_say_errors_carry_their_code(sock_dir, monkeypatch, capsys):
    async def handler(msg, send):
        await send({"ok": False, "error": "no call in progress", "code": 11})

    assert run_with(sock_dir, monkeypatch, handler, ["say", "hello"]) == 11
    assert "error: no call in progress" in capsys.readouterr().out


def test_hangup_with_say(sock_dir, monkeypatch, capsys):
    async def handler(msg, send):
        assert msg == {"cmd": "hangup", "call_id": None, "say": "Bye."}
        await send({"ok": True, "result": {**RESULT, "ended_by": "hangup_command"}})

    assert run_with(sock_dir, monkeypatch, handler, ["hangup", "--say", "Bye."]) == 0
    assert "hung up on request" in capsys.readouterr().out


def test_daemon_not_running_in_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PHONE_HOME", str(tmp_path))
    assert main(["listen", "--json"]) == 9
    assert json.loads(capsys.readouterr().out)["code"] == 9


@pytest.mark.parametrize("argv", [["listen", "--json", "--timeout-sec", "-20"],
                                  ["listen", "--json", "--timeout-sec", "nan"],
                                  ["listen", "--json", "--timeout-sec", "3601"],
                                  ["call", "+15550000000", "--json", "--yes", "--prompt-file", "/nonexistent/prompt.txt"],
                                  ["call", "+15550000000", "--json", "--yes", "--prompt", "x", "--prompt-file", "/dev/null"]])
def test_bad_local_arguments_are_json_usage_errors(sock_dir, monkeypatch, capsys, argv):
    async def handler(msg, send):
        raise AssertionError("the daemon must not be contacted")

    assert run_with(sock_dir, monkeypatch, handler, argv) == 2
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"] is False and reply["code"] == 2 and reply["error"]


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_hangup_reply_without_a_result_still_prints_one_line(sock_dir, monkeypatch, capsys, flags):
    async def handler(msg, send):
        await send({"ok": True, "hung_up": True})

    assert run_with(sock_dir, monkeypatch, handler, ["hangup", *flags]) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    if flags:
        assert json.loads(out)["ok"] is True


CANCELLED = {**RESULT, "outcome": "cancelled", "ended_by": "hangup_command", "answered_at": None, "duration_sec": None,
             "transcript": []}


@pytest.mark.parametrize("ended, expected", [
    ({"event": "ended", "reason": "busy", "detail": "486", "exit_code": 5, "call_id": "c7f3a2", "result": None},
     {"ok": False, "outcome": "busy", "detail": "486", "code": 5}),
    ({"event": "ended", "reason": "cancelled", "detail": "", "exit_code": 7, "call_id": "c7f3a2", "result": CANCELLED},
     {**CANCELLED, "ok": False, "outcome": "cancelled", "detail": "", "code": 7}),
    ({"event": "ended", "reason": "no_answer", "exit_code": 6},
     {"ok": False, "outcome": "no_answer", "detail": "", "code": 6}),
])
def test_unsuccessful_calls_have_one_json_shape(sock_dir, monkeypatch, capsys, ended, expected):
    async def handler(msg, send):
        await send({"event": "dialling", "uri": "x"})
        await send(ended)

    assert run_with(sock_dir, monkeypatch, handler, ["call", "+15550000000", "--json", "--yes"]) == expected["code"]
    assert json.loads(capsys.readouterr().out) == expected


def test_relay_call_json(sock_dir, monkeypatch, capsys):
    async def handler(msg, send):
        await send({"event": "answered"})
        await send({"ok": True, "event": "answered", "call_id": "c7f3a2", "mode": "relay"})

    assert run_with(sock_dir, monkeypatch, handler, ["call", "+15550000000", "--relay", "--json", "--yes"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "call_id": "c7f3a2", "mode": "relay", "outcome": "answered"}


@pytest.mark.parametrize("reply", [{"ok": True, "kind": "bogus"}, {"ok": True}])
@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_listen_with_an_unknown_reply_kind_is_a_protocol_error(sock_dir, monkeypatch, capsys, reply, flags):
    async def handler(msg, send):
        await send(reply)

    assert run_with(sock_dir, monkeypatch, handler, ["listen", *flags]) == 9
    out = capsys.readouterr().out
    if flags:
        assert json.loads(out) == {"ok": False, "error": "unexpected reply from the daemon", "code": 9}
    else:
        assert out == "error: unexpected reply from the daemon\n"


@pytest.mark.parametrize("argv, carries_on", [(["listen", "--json"], False), (["listen"], False),
                                              (["say", "--json", "hi"], True), (["hangup", "--json", "--say", "Bye."], True)])
def test_ctrl_c_while_waiting_exits_130(sock_dir, monkeypatch, capsys, argv, carries_on):
    import asyncio
    import signal
    import threading
    main_thread = threading.main_thread().ident
    finished, cancelled = threading.Event(), threading.Event()

    async def handler(msg, send):
        signal.pthread_kill(main_thread, signal.SIGINT)   # the user presses Ctrl-C while the CLI waits
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finished.set()
        await send({"ok": True, "result": "said"})        # nobody is listening any more: a no-op

    monkeypatch.setenv("AI_PHONE_HOME", str(sock_dir))
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        stop, t = _serve(sock_dir / "control.sock", handler)
        try:
            assert main(argv) == 130
            captured = capsys.readouterr()
            if "--json" in argv:
                assert json.loads(captured.out) == {"ok": False, "error": "interrupted", "code": 130}
            else:
                assert captured.out == "error: interrupted\n"
            assert "Traceback" not in captured.err
            if carries_on:   # say / hangup: the daemon finishes what it was asked to do
                assert finished.wait(3) and not cancelled.is_set()
            else:            # listen: the server cancels it at once and frees the listener slot
                assert cancelled.wait(3) and not finished.is_set()
        finally:
            stop.set(); t.join(2)
    finally:
        signal.signal(signal.SIGINT, previous)


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_a_broken_config_in_the_divert_check_is_a_usage_error(sock_dir, monkeypatch, capsys, flags):
    monkeypatch.setenv("AI_PHONE_HOME", str(sock_dir))
    (sock_dir / "config.yaml").write_text("lines: [\n")
    assert main(["call", "+15550000000", *flags]) == 2
    out = capsys.readouterr().out
    if flags:
        reply = json.loads(out)
        assert reply["ok"] is False and reply["code"] == 2 and "not valid YAML" in reply["error"]
    else:
        assert out.startswith("error: config:") and len(out.splitlines()) >= 1
