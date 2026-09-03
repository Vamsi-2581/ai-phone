"""Newline-delimited JSON over ~/.ai-phone/control.sock. One `call` at a time; `status` and `hangup` always."""
EXIT = {"answered": 0, "busy": 5, "line_busy": 5, "no_answer": 6, "rejected": 7, "redirected": 8, "daemon_gone": 9}
COMMANDS = ("status", "call", "hangup")
