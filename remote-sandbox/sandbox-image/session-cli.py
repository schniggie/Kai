#!/usr/bin/env python3
"""CLI used by the orchestrator via `docker exec` to talk to kai-sessiond."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

SOCK_PATH = Path(os.environ.get("KAI_SESSION_SOCK", "/run/kai/sessiond.sock"))


def call(payload: dict) -> dict:
    if not SOCK_PATH.exists():
        return {"ok": False, "error": f"session daemon socket missing: {SOCK_PATH}"}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(SOCK_PATH))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8") or "{}")
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="kai-session")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping")

    p_exec = sub.add_parser("exec")
    p_exec.add_argument("--session-id", default="default")
    p_exec.add_argument("--timeout", type=float, default=30)
    p_exec.add_argument("command")

    p_close = sub.add_parser("close")
    p_close.add_argument("--session-id", required=True)

    sub.add_parser("list")

    args = parser.parse_args()
    if args.cmd == "ping":
        resp = call({"action": "ping"})
    elif args.cmd == "exec":
        resp = call(
            {
                "action": "exec",
                "session_id": args.session_id,
                "timeout": args.timeout,
                "command": args.command,
            }
        )
    elif args.cmd == "close":
        resp = call({"action": "close", "session_id": args.session_id})
    elif args.cmd == "list":
        resp = call({"action": "list"})
    else:
        resp = {"ok": False, "error": "unknown"}

    sys.stdout.write(json.dumps(resp))
    sys.stdout.write("\n")
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
