#!/usr/bin/env python3
"""Persistent bash session daemon for Kai remote sandboxes.

Listens on a Unix socket. The orchestrator talks to it via `kai-session` over
`docker exec`. Each session is a long-lived `bash --noprofile --norc` with
sentinel framing on stderr (same idea as Android's PersistentSandboxShell).
"""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

SOCK_PATH = Path(os.environ.get("KAI_SESSION_SOCK", "/run/kai/sessiond.sock"))
HOME = Path(os.environ.get("HOME", "/root"))
START_SENTINEL = "___KAI_START___"
END_SENTINEL_PREFIX = "___KAI_END___:"
MAX_OUTPUT = 100_000


class Session:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(
            ["bash", "--noprofile", "--norc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(HOME),
            env={
                "HOME": str(HOME),
                "USER": "root",
                "LOGNAME": "root",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "TERM": "xterm-256color",
                "LANG": "C.UTF-8",
            },
            text=True,
            bufsize=0,
        )
        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        # Line-buffer friendly mode for children that honor it.
        self._write("export PYTHONUNBUFFERED=1\n")

    def _write(self, data: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        if not self.alive():
            return
        try:
            self._write("exit\n")
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()

    def exec(self, command: str, timeout: float) -> dict:
        with self.lock:
            if not self.alive():
                raise RuntimeError("session dead")

            token = uuid.uuid4().hex
            start = f"{START_SENTINEL}{token}"
            end = f"{END_SENTINEL_PREFIX}{token}:"
            # Run in a subshell so `exit` inside the command cannot kill the session.
            # Capture exit code on stderr via the end sentinel.
            script = (
                f"printf '%s\\n' '{start}' >&2\n"
                f"(\n{command}\n)\n"
                f"__kai_ec=$?\n"
                f"printf '%s%d\\n' '{end}' \"$__kai_ec\" >&2\n"
            )
            self._write(script)

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            exit_code = 0
            deadline = time.monotonic() + timeout
            collecting = False
            stdout_buf = ""
            stderr_buf = ""

            assert self.proc.stdout and self.proc.stderr
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Best-effort interrupt; next call may need a fresh session.
                    try:
                        self.proc.send_signal(signal.SIGINT)
                    except Exception:
                        pass
                    return {
                        "stdout": "".join(stdout_chunks)[-MAX_OUTPUT:],
                        "stderr": ("".join(stderr_chunks) + "\n[kai] command timed out")[-MAX_OUTPUT:],
                        "exit_code": 124,
                        "timed_out": True,
                    }

                ready, _, _ = select.select(
                    [self.proc.stdout, self.proc.stderr],
                    [],
                    [],
                    min(0.2, remaining),
                )
                if not ready:
                    if not self.alive():
                        raise RuntimeError("session died during command")
                    continue

                if self.proc.stdout in ready:
                    data = os.read(self.proc.stdout.fileno(), 8192).decode("utf-8", "replace")
                    if data:
                        stdout_buf += data
                        while "\n" in stdout_buf or len(stdout_buf) > 4096:
                            if "\n" in stdout_buf:
                                line, stdout_buf = stdout_buf.split("\n", 1)
                                line += "\n"
                            else:
                                line, stdout_buf = stdout_buf, ""
                            if collecting:
                                stdout_chunks.append(line)

                if self.proc.stderr in ready:
                    data = os.read(self.proc.stderr.fileno(), 8192).decode("utf-8", "replace")
                    if data:
                        stderr_buf += data
                        while "\n" in stderr_buf:
                            line, stderr_buf = stderr_buf.split("\n", 1)
                            line += "\n"
                            if start in line:
                                collecting = True
                                continue
                            if end in line and collecting:
                                try:
                                    exit_code = int(line.strip().split(end, 1)[1])
                                except ValueError:
                                    exit_code = 1
                                # Flush any trailing stdout without a newline.
                                if stdout_buf and collecting:
                                    stdout_chunks.append(stdout_buf)
                                    stdout_buf = ""
                                return {
                                    "stdout": "".join(stdout_chunks)[-MAX_OUTPUT:],
                                    "stderr": "".join(stderr_chunks)[-MAX_OUTPUT:],
                                    "exit_code": exit_code,
                                    "timed_out": False,
                                }
                            if collecting and start not in line:
                                stderr_chunks.append(line)


class Daemon:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()

    def handle(self, req: dict) -> dict:
        action = req.get("action")
        if action == "ping":
            return {"ok": True, "sessions": list(self.sessions)}
        if action == "exec":
            session_id = str(req.get("session_id") or "default")
            command = str(req.get("command") or "")
            timeout = float(req.get("timeout") or 30)
            timeout = max(1.0, min(timeout, 300.0))
            if not command:
                return {"ok": False, "error": "command required"}
            session = self._session(session_id)
            try:
                result = session.exec(command, timeout)
                return {"ok": True, **result}
            except Exception as exc:
                self._drop(session_id)
                session = self._session(session_id)
                result = session.exec(command, timeout)
                result["recovered"] = True
                result["recovery_error"] = str(exc)
                return {"ok": True, **result}
        if action == "close":
            session_id = str(req.get("session_id") or "")
            self._drop(session_id)
            return {"ok": True}
        if action == "list":
            with self.lock:
                return {
                    "ok": True,
                    "sessions": [
                        {"id": sid, "alive": s.alive()} for sid, s in self.sessions.items()
                    ],
                }
        return {"ok": False, "error": f"unknown action: {action}"}

    def _session(self, session_id: str) -> Session:
        with self.lock:
            existing = self.sessions.get(session_id)
            if existing and existing.alive():
                return existing
            if existing:
                existing.close()
            session = Session(session_id)
            self.sessions[session_id] = session
            return session

    def _drop(self, session_id: str) -> None:
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if session:
            session.close()


def serve() -> None:
    SOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCK_PATH.exists():
        SOCK_PATH.unlink()

    daemon = Daemon()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCK_PATH))
    os.chmod(SOCK_PATH, 0o600)
    server.listen(32)
    print(f"kai-sessiond listening on {SOCK_PATH}", flush=True)

    def client_thread(conn: socket.socket) -> None:
        try:
            with conn:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    return
                req = json.loads(buf.decode("utf-8"))
                resp = daemon.handle(req)
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except Exception as exc:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode("utf-8"))
            except Exception:
                pass

    while True:
        conn, _ = server.accept()
        threading.Thread(target=client_thread, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    serve()
