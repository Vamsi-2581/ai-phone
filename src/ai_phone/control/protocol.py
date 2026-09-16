"""Newline-delimited JSON over ~/.ai-phone/control.sock. One call in progress at a time; the other commands run
alongside it."""
import re

EXIT = {"answered": 0, "completed": 0, "usage": 2, "busy": 5, "line_busy": 5, "no_answer": 6, "rejected": 7,
        "redirected": 8, "daemon_gone": 9, "listen_timeout": 10, "no_call": 11, "listener_busy": 12, "blocked": 13,
        "daily_limit": 14}
COMMANDS = ("status", "call", "hangup", "listen", "say")
MAX_PROMPT_CHARS = 4000
MAX_SAY_CHARS = 2000
CALL_ID_RE = re.compile(r"\A[0-9a-f]{6}\Z")
