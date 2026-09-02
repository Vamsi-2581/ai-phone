"""Newline-free JSON messages over a multiprocessing Connection (one message per send_bytes)."""
from __future__ import annotations

import json
from multiprocessing.connection import Connection


def send(conn: Connection, msg: dict) -> None:
    conn.send_bytes(json.dumps(msg, separators=(",", ":")).encode())


def recv(conn: Connection) -> dict:
    return json.loads(conn.recv_bytes().decode())
