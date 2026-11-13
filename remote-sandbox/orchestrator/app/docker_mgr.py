from __future__ import annotations

import hashlib
import json
import shlex
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container

# Hard caps so one token cannot exhaust the shared orchestrator.
MAX_FS_BYTES = 5_000_000
MAX_FS_LIST_ENTRIES = 2_000
MAX_EXEC_OUTPUT_BYTES = 2_000_000
MAX_JOB_OUTPUT_CHARS = 100_000
MAX_ACTIVE_JOBS_PER_SANDBOX = 4
MAX_STORED_JOBS_PER_SANDBOX = 32
JOB_TTL_SECONDS = 3600
DEFAULT_JOB_TIMEOUT_SECONDS = 3600
MAX_SANDBOX_CONTAINERS = int(__import__("os").environ.get("MAX_SANDBOX_CONTAINERS", "16"))
MAX_SANDBOXES_PER_TOKEN = int(__import__("os").environ.get("MAX_SANDBOXES_PER_TOKEN", "4"))


class SandboxLimitError(RuntimeError):
    pass


def sandbox_id_for(token_id: str, device_id: str) -> str:
    digest = hashlib.sha256(f"{token_id}:{device_id}".encode()).hexdigest()[:16]
    return f"sbx_{digest}"


def container_name_for(sandbox_id: str) -> str:
    return f"kai-{sandbox_id}"


def volume_name_for(sandbox_id: str) -> str:
    return f"kai-{sandbox_id}-data"


class JobLimitError(RuntimeError):
    pass


@dataclass
class BackgroundJob:
    job_id: str
    sandbox_id: str
    command: str
    status: str = "running"
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exec_id: str | None = None
    pid_file: str | None = None


class DockerManager:
    def __init__(
        self,
        image: str,
        network_name: str,
        idle_timeout_seconds: int,
    ) -> None:
        self.client = docker.from_env()
        self.image = image
        self.network_name = network_name
        self.idle_timeout_seconds = idle_timeout_seconds
        self._jobs: dict[str, BackgroundJob] = {}
        self._jobs_lock = threading.Lock()
        self._last_used: dict[str, float] = {}
        self._ensure_network()
        threading.Thread(target=self._idle_reaper, daemon=True).start()

    def _ensure_network(self) -> None:
        try:
            self.client.networks.get(self.network_name)
        except NotFound:
            self.client.networks.create(self.network_name, driver="bridge", check_duplicate=True)

    def acquire(self, token_id: str, device_id: str) -> dict[str, Any]:
        sandbox_id = sandbox_id_for(token_id, device_id)
        name = container_name_for(sandbox_id)
        volume = volume_name_for(sandbox_id)

        container = self._get_container(name)
        if container is None:
            # Count before creating either the volume or the container so a
            # rejected acquire cannot leave orphan volumes behind.
            existing = self._count_sandbox_containers()
            if existing >= MAX_SANDBOX_CONTAINERS:
                raise SandboxLimitError(
                    f"at most {MAX_SANDBOX_CONTAINERS} sandbox containers allowed"
                )
            per_token = self._count_sandbox_containers(token_id=token_id)
            if per_token >= MAX_SANDBOXES_PER_TOKEN:
                raise SandboxLimitError(
                    f"at most {MAX_SANDBOXES_PER_TOKEN} sandboxes per token"
                )
            if not self._volume_exists(volume):
                self.client.volumes.create(
                    name=volume,
                    labels={
                        "kai.sandbox": "1",
                        "kai.sandbox_id": sandbox_id,
                        "kai.token_id": token_id,
                        "kai.device_id": device_id,
                    },
                )
            container = self.client.containers.run(
                image=self.image,
                name=name,
                detach=True,
                tty=False,
                stdin_open=False,
                network=self.network_name,
                volumes={volume: {"bind": "/root", "mode": "rw"}},
                labels={
                    "kai.sandbox": "1",
                    "kai.sandbox_id": sandbox_id,
                    "kai.token_id": token_id,
                    "kai.device_id": device_id,
                },
                mem_limit="2g",
                nano_cpus=2_000_000_000,
                restart_policy={"Name": "unless-stopped"},
            )
        else:
            container.reload()
            if container.status != "running":
                container.start()

        self._wait_ready(container)
        self.touch(sandbox_id)
        return self.status(token_id, device_id)

    def status(self, token_id: str, device_id: str) -> dict[str, Any]:
        sandbox_id = sandbox_id_for(token_id, device_id)
        name = container_name_for(sandbox_id)
        container = self._get_container(name)
        ready = False
        disk = 0
        state = "missing"
        if container is not None:
            container.reload()
            state = container.status
            if container.status == "running":
                ready = self._ping_sessiond(container)
                disk = self._disk_usage(container)
        return {
            "sandbox_id": sandbox_id,
            "device_id": device_id,
            "status": state,
            "container_name": name,
            "ready": ready,
            "disk_usage_bytes": disk,
        }

    def destroy(self, token_id: str, device_id: str, wipe_volume: bool = False) -> None:
        sandbox_id = sandbox_id_for(token_id, device_id)
        name = container_name_for(sandbox_id)
        container = self._get_container(name)
        if container is not None:
            try:
                container.remove(force=True)
            except APIError:
                pass
        if wipe_volume:
            vol_name = volume_name_for(sandbox_id)
            try:
                self.client.volumes.get(vol_name).remove(force=True)
            except Exception:
                pass
        self._last_used.pop(sandbox_id, None)

    def touch(self, sandbox_id: str) -> None:
        self._last_used[sandbox_id] = time.time()

    def exec_command(
        self,
        token_id: str,
        device_id: str,
        command: str,
        session_id: str,
        timeout: int,
        working_dir: str | None,
        env: dict[str, str] | None,
        fresh: bool,
    ) -> dict[str, Any]:
        info = self.acquire(token_id, device_id)
        sandbox_id = info["sandbox_id"]
        container = self._require_running(sandbox_id)

        wrapped = command
        prefixes: list[str] = []
        if working_dir:
            prefixes.append(f"cd {shlex.quote(working_dir)}")
        if env:
            for key, value in env.items():
                if not key.isidentifier():
                    continue
                prefixes.append(f"export {key}={shlex.quote(value)}")
        if prefixes:
            wrapped = " && ".join(prefixes + [f"( {command} )"])

        if fresh:
            result = self._docker_exec(
                container,
                [
                    "timeout",
                    "--signal=KILL",
                    str(timeout),
                    "bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    wrapped,
                ],
                timeout=timeout + 5,
            )
            self.touch(sandbox_id)
            return {
                "stdout": _truncate_chars(result["stdout"], MAX_JOB_OUTPUT_CHARS),
                "stderr": _truncate_chars(result["stderr"], MAX_JOB_OUTPUT_CHARS),
                "exit_code": 124 if result["timed_out"] else result["exit_code"],
                "timed_out": result["timed_out"] or result["exit_code"] == 124,
                "session_id": session_id,
            }

        payload = {
            "action": "exec",
            "session_id": session_id,
            "timeout": timeout,
            "command": wrapped,
        }
        raw = self._docker_exec(
            container,
            ["kai-session", "exec", "--session-id", session_id, "--timeout", str(timeout), wrapped],
            timeout=timeout + 5,
        )
        # Prefer structured JSON from kai-session when present.
        try:
            parsed = json.loads(raw["stdout"] or "{}")
            if parsed.get("ok"):
                self.touch(sandbox_id)
                return {
                    "stdout": _truncate_chars(str(parsed.get("stdout", "")), MAX_JOB_OUTPUT_CHARS),
                    "stderr": _truncate_chars(str(parsed.get("stderr", "")), MAX_JOB_OUTPUT_CHARS),
                    "exit_code": int(parsed.get("exit_code", 0)),
                    "timed_out": bool(parsed.get("timed_out", False)),
                    "session_id": session_id,
                }
            err = parsed.get("error") or raw["stderr"] or "session exec failed"
            self.touch(sandbox_id)
            return {
                "stdout": "",
                "stderr": str(err),
                "exit_code": 1,
                "timed_out": False,
                "session_id": session_id,
            }
        except json.JSONDecodeError:
            self.touch(sandbox_id)
            return {
                "stdout": raw["stdout"],
                "stderr": raw["stderr"] or f"bad session response; payload={payload}",
                "exit_code": raw["exit_code"],
                "timed_out": raw["timed_out"],
                "session_id": session_id,
            }

    def close_session(self, token_id: str, device_id: str, session_id: str) -> None:
        sandbox_id = sandbox_id_for(token_id, device_id)
        container = self._get_container(container_name_for(sandbox_id))
        if container is None:
            return
        self._docker_exec(container, ["kai-session", "close", "--session-id", session_id], timeout=10)
        self.touch(sandbox_id)

    def start_job(
        self,
        token_id: str,
        device_id: str,
        command: str,
        working_dir: str | None,
        env: dict[str, str] | None,
    ) -> BackgroundJob:
        info = self.acquire(token_id, device_id)
        sandbox_id = info["sandbox_id"]
        container = self._require_running(sandbox_id)
        self._expire_jobs_locked(sandbox_id)
        with self._jobs_lock:
            active = sum(
                1
                for j in self._jobs.values()
                if j.sandbox_id == sandbox_id and j.status == "running"
            )
            if active >= MAX_ACTIVE_JOBS_PER_SANDBOX:
                raise JobLimitError(
                    f"at most {MAX_ACTIVE_JOBS_PER_SANDBOX} active jobs per sandbox"
                )
            job_id = f"job_{uuid.uuid4().hex[:12]}"
            pid_file = f"/tmp/kai-job-{job_id}.pid"
            job = BackgroundJob(
                job_id=job_id,
                sandbox_id=sandbox_id,
                command=command,
                pid_file=pid_file,
            )
            self._jobs[job_id] = job

        def runner() -> None:
            prefixes: list[str] = []
            if working_dir:
                prefixes.append(f"cd {shlex.quote(working_dir)}")
            if env:
                for key, value in env.items():
                    if key.isidentifier():
                        prefixes.append(f"export {key}={shlex.quote(value)}")
            inner = " && ".join(prefixes + [f"( {command} )"]) if prefixes else command
            # Record the bash PID so kill_job can signal the real process tree.
            wrapped = (
                f"echo $$ > {shlex.quote(pid_file)}; "
                f"exec bash --noprofile --norc -c {shlex.quote(inner)}"
            )
            result = self._docker_exec(
                container,
                ["timeout", "--signal=KILL", str(DEFAULT_JOB_TIMEOUT_SECONDS), "bash", "--noprofile", "--norc", "-c", wrapped],
                timeout=DEFAULT_JOB_TIMEOUT_SECONDS + 5,
                job=job,
            )
            with self._jobs_lock:
                if job.status == "killed":
                    return
                job.status = "timed_out" if result["timed_out"] else "finished"
                job.exit_code = result["exit_code"]
                job.stdout = _truncate_chars(result["stdout"], MAX_JOB_OUTPUT_CHARS)
                job.stderr = _truncate_chars(result["stderr"], MAX_JOB_OUTPUT_CHARS)
                job.finished_at = time.time()
            self.touch(sandbox_id)
            self._expire_jobs_locked(sandbox_id)

        threading.Thread(target=runner, daemon=True, name=f"kai-job-{job_id}").start()
        self.touch(sandbox_id)
        return job

    def list_jobs(self, sandbox_id: str) -> list[BackgroundJob]:
        self._expire_jobs_locked(sandbox_id)
        with self._jobs_lock:
            return [j for j in self._jobs.values() if j.sandbox_id == sandbox_id]

    def get_job(self, job_id: str) -> BackgroundJob | None:
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def kill_job(self, job_id: str) -> BackgroundJob | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            sandbox_id = job.sandbox_id
            pid_file = job.pid_file
            job.status = "killed"
            job.exit_code = -9
            job.finished_at = time.time()
        container = self._get_container(container_name_for(sandbox_id))
        if container is not None and pid_file:
            # Best-effort: kill the recorded PID and its children.
            self._docker_exec(
                container,
                [
                    "bash",
                    "-c",
                    f"if [ -f {shlex.quote(pid_file)} ]; then "
                    f"kill -TERM -$(cat {shlex.quote(pid_file)}) 2>/dev/null || "
                    f"kill -TERM $(cat {shlex.quote(pid_file)}) 2>/dev/null; "
                    f"sleep 0.2; "
                    f"kill -KILL -$(cat {shlex.quote(pid_file)}) 2>/dev/null || "
                    f"kill -KILL $(cat {shlex.quote(pid_file)}) 2>/dev/null; "
                    f"rm -f {shlex.quote(pid_file)}; fi",
                ],
                timeout=10,
            )
        return job

    def remove_job(self, job_id: str) -> bool:
        with self._jobs_lock:
            return self._jobs.pop(job_id, None) is not None

    def list_fs(self, token_id: str, device_id: str, path: str) -> list[dict[str, Any]]:
        info = self.acquire(token_id, device_id)
        container = self._require_running(info["sandbox_id"])
        script = f"""
import json, os, sys
path = {path!r}
limit = {MAX_FS_LIST_ENTRIES}
entries = []
try:
    names = sorted(os.listdir(path), key=lambda n: (not os.path.isdir(os.path.join(path, n)), n.lower()))
    for name in names[:limit]:
        full = os.path.join(path, name)
        try:
            st = os.lstat(full)
            entries.append({{
                "name": name,
                "path": full,
                "is_directory": os.path.isdir(full),
                "size_bytes": int(st.st_size),
                "last_modified_ms": int(st.st_mtime * 1000),
            }})
        except OSError:
            pass
except Exception as e:
    print(json.dumps({{"error": str(e)}}))
    sys.exit(1)
print(json.dumps(entries))
"""
        result = self._docker_exec(container, ["python3", "-c", script], timeout=30)
        self.touch(info["sandbox_id"])
        if result["exit_code"] != 0:
            raise FileNotFoundError(result["stderr"] or result["stdout"] or "list failed")
        return json.loads(result["stdout"] or "[]")

    def read_fs(self, token_id: str, device_id: str, path: str, max_bytes: int) -> bytes:
        info = self.acquire(token_id, device_id)
        container = self._require_running(info["sandbox_id"])
        capped = min(max(1, max_bytes), MAX_FS_BYTES)
        result = self._docker_exec(
            container,
            ["python3", "-c", f"import sys; p={path!r}; d=open(p,'rb').read({capped+1}); sys.stdout.buffer.write(d)"],
            timeout=60,
            binary=True,
        )
        self.touch(info["sandbox_id"])
        if result["exit_code"] != 0:
            raise FileNotFoundError(result["stderr"] or "read failed")
        return result["stdout_bytes"][:capped]

    def write_fs(self, token_id: str, device_id: str, path: str, content: bytes) -> None:
        if len(content) > MAX_FS_BYTES:
            raise OSError(f"payload exceeds {MAX_FS_BYTES} bytes")
        info = self.acquire(token_id, device_id)
        container = self._require_running(info["sandbox_id"])
        # Stage as chunked base64 under /tmp so we never put multi-MB argv on
        # the orchestrator or docker exec create call.
        import base64

        marker = f"/tmp/kai-upload-{uuid.uuid4().hex}"
        b64 = base64.b64encode(content).decode("ascii")
        clear = self._docker_exec(container, ["bash", "-c", f": > {shlex.quote(marker)}.b64"], timeout=10)
        if clear["exit_code"] != 0:
            raise OSError(clear["stderr"] or "failed to stage upload")
        chunk_size = 48_000
        for i in range(0, len(b64), chunk_size):
            piece = b64[i : i + chunk_size]
            append = self._docker_exec(
                container,
                ["bash", "-c", f"printf '%s' {shlex.quote(piece)} >> {shlex.quote(marker)}.b64"],
                timeout=15,
            )
            if append["exit_code"] != 0:
                self._docker_exec(container, ["rm", "-f", marker + ".b64"], timeout=10)
                raise OSError(append["stderr"] or "failed to stage upload chunk")
        script = (
            "import base64, os\n"
            f"path = {path!r}\n"
            f"src = {marker + '.b64'!r}\n"
            "os.makedirs(os.path.dirname(path) or '.', exist_ok=True)\n"
            "data = base64.b64decode(open(src, 'rb').read())\n"
            f"if len(data) > {MAX_FS_BYTES}:\n"
            "    raise SystemExit('payload too large')\n"
            "open(path, 'wb').write(data)\n"
        )
        result = self._docker_exec(container, ["python3", "-c", script], timeout=60)
        self._docker_exec(container, ["rm", "-f", marker + ".b64"], timeout=10)
        self.touch(info["sandbox_id"])
        if result["exit_code"] != 0:
            raise OSError(result["stderr"] or "write failed")

    def delete_fs(self, token_id: str, device_id: str, path: str, recursive: bool) -> None:
        info = self.acquire(token_id, device_id)
        container = self._require_running(info["sandbox_id"])
        flag = "-rf" if recursive else "-f"
        result = self._docker_exec(container, ["rm", flag, "--", path], timeout=60)
        self.touch(info["sandbox_id"])
        if result["exit_code"] != 0:
            raise OSError(result["stderr"] or "delete failed")

    def rename_fs(self, token_id: str, device_id: str, path: str, new_name: str) -> str:
        info = self.acquire(token_id, device_id)
        container = self._require_running(info["sandbox_id"])
        script = f"""
import os, sys
path = {path!r}
new_name = {new_name!r}
if '/' in new_name or new_name in ('', '.', '..'):
    print('invalid'); sys.exit(2)
parent = os.path.dirname(path) or '/'
dest = os.path.join(parent, new_name)
if os.path.exists(dest):
    print('collision'); sys.exit(3)
os.rename(path, dest)
print(dest)
"""
        result = self._docker_exec(container, ["python3", "-c", script], timeout=30)
        self.touch(info["sandbox_id"])
        if result["exit_code"] == 3:
            raise FileExistsError("collision")
        if result["exit_code"] != 0:
            raise OSError(result["stderr"] or result["stdout"] or "rename failed")
        return (result["stdout"] or "").strip()

    def _get_container(self, name: str) -> Container | None:
        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def _require_running(self, sandbox_id: str) -> Container:
        container = self._get_container(container_name_for(sandbox_id))
        if container is None:
            raise RuntimeError("sandbox container missing")
        container.reload()
        if container.status != "running":
            container.start()
            self._wait_ready(container)
        return container

    def _wait_ready(self, container: Container, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        last_err = "not ready"
        while time.time() < deadline:
            container.reload()
            if container.status == "running" and self._ping_sessiond(container):
                return
            last_err = container.status
            time.sleep(0.4)
        raise RuntimeError(f"sandbox not ready: {last_err}")

    def _ping_sessiond(self, container: Container) -> bool:
        try:
            result = self._docker_exec(container, ["kai-session", "ping"], timeout=5)
            if result["exit_code"] != 0:
                return False
            data = json.loads(result["stdout"] or "{}")
            return bool(data.get("ok"))
        except Exception:
            return False

    def _disk_usage(self, container: Container) -> int:
        result = self._docker_exec(
            container,
            ["bash", "-c", "du -sb /root 2>/dev/null | cut -f1"],
            timeout=15,
        )
        try:
            return int((result["stdout"] or "0").strip() or "0")
        except ValueError:
            return 0

    def _volume_exists(self, name: str) -> bool:
        try:
            self.client.volumes.get(name)
            return True
        except NotFound:
            return False

    def _docker_exec(
        self,
        container: Container,
        cmd: list[str],
        timeout: int,
        binary: bool = False,
        job: BackgroundJob | None = None,
    ) -> dict[str, Any]:
        """Run a command with a wall-clock deadline and a hard output byte cap.

        The stream reader runs on a side thread so a silent (no-output) command
        still hits the deadline — a plain `for chunk in stream` would block forever.
        """
        import queue

        exec_id = self.client.api.exec_create(
            container.id,
            cmd,
            stdout=True,
            stderr=True,
        )["Id"]
        if job is not None:
            with self._jobs_lock:
                job.exec_id = exec_id

        stream = self.client.api.exec_start(exec_id, demux=True, stream=True)
        stdout_b = bytearray()
        stderr_b = bytearray()
        timed_out = False
        truncated = False
        deadline = time.time() + max(1, timeout)
        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        def _reader() -> None:
            try:
                for chunk in stream:
                    events.put(("chunk", chunk))
            except Exception as exc:  # noqa: BLE001
                events.put(("error", exc))
            finally:
                events.put(("done", None))

        reader = threading.Thread(target=_reader, daemon=True, name=f"kai-exec-{exec_id[:8]}")
        reader.start()

        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                kind, payload = events.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue
            if kind == "done":
                break
            if kind == "error":
                break
            chunk = payload
            if not chunk:
                continue
            out_part, err_part = chunk if isinstance(chunk, tuple) else (chunk, b"")
            if out_part:
                if len(stdout_b) < MAX_EXEC_OUTPUT_BYTES:
                    remain = MAX_EXEC_OUTPUT_BYTES - len(stdout_b)
                    stdout_b.extend(out_part[:remain])
                    if len(out_part) > remain:
                        truncated = True
                else:
                    truncated = True
            if err_part:
                if len(stderr_b) < MAX_EXEC_OUTPUT_BYTES:
                    remain = MAX_EXEC_OUTPUT_BYTES - len(stderr_b)
                    stderr_b.extend(err_part[:remain])
                    if len(err_part) > remain:
                        truncated = True
                else:
                    truncated = True

        inspect = self.client.api.exec_inspect(exec_id)
        running = bool(inspect.get("Running"))
        exit_code = int(inspect.get("ExitCode") or 0)
        if timed_out or running:
            timed_out = True
            exit_code = 124
            self._kill_exec(inspect)
        if truncated and not binary:
            stderr_b.extend(b"\n[kai] output truncated\n")
        if binary:
            return {
                "stdout_bytes": bytes(stdout_b),
                "stdout": "",
                "stderr": bytes(stderr_b).decode("utf-8", "replace"),
                "exit_code": exit_code,
                "timed_out": timed_out,
            }
        return {
            "stdout": bytes(stdout_b).decode("utf-8", "replace"),
            "stderr": bytes(stderr_b).decode("utf-8", "replace"),
            "exit_code": exit_code,
            "timed_out": timed_out,
        }

    def _kill_exec(self, inspect: dict[str, Any]) -> None:
        """Stop a still-running docker exec after a stream deadline."""
        import os
        import signal

        pid = inspect.get("Pid")
        if isinstance(pid, int) and pid > 1:
            try:
                os.kill(pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return
            except PermissionError:
                pass
        # Fallback: nothing else we can do without container context.

    def _count_sandbox_containers(self, token_id: str | None = None) -> int:
        """Count live sandbox containers. Idle-stopped ones are removed, so all=True is fine."""
        try:
            filters: dict[str, Any] = {"label": ["kai.sandbox=1"]}
            if token_id is not None:
                filters = {"label": [f"kai.sandbox=1", f"kai.token_id={token_id}"]}
            return len(self.client.containers.list(all=True, filters=filters))
        except APIError:
            return 0

    def _expire_jobs_locked(self, sandbox_id: str) -> None:
        now = time.time()
        with self._jobs_lock:
            owned = [j for j in self._jobs.values() if j.sandbox_id == sandbox_id]
            for job in owned:
                if job.status == "running":
                    continue
                finished = job.finished_at or job.started_at
                if now - finished > JOB_TTL_SECONDS:
                    self._jobs.pop(job.job_id, None)
            owned = [j for j in self._jobs.values() if j.sandbox_id == sandbox_id]
            finished = sorted(
                (j for j in owned if j.status != "running"),
                key=lambda j: j.finished_at or j.started_at,
            )
            overflow = len(finished) - (MAX_STORED_JOBS_PER_SANDBOX - MAX_ACTIVE_JOBS_PER_SANDBOX)
            for job in finished[: max(0, overflow)]:
                self._jobs.pop(job.job_id, None)

    def _idle_reaper(self) -> None:
        while True:
            time.sleep(60)
            # Drop expired job records globally.
            for sandbox_id in {j.sandbox_id for j in list(self._jobs.values())}:
                self._expire_jobs_locked(sandbox_id)
            if self.idle_timeout_seconds <= 0:
                continue
            now = time.time()
            for sandbox_id, last in list(self._last_used.items()):
                if now - last < self.idle_timeout_seconds:
                    continue
                name = container_name_for(sandbox_id)
                container = self._get_container(name)
                if container is None:
                    self._last_used.pop(sandbox_id, None)
                    continue
                try:
                    container.reload()
                    if container.status == "running":
                        container.stop(timeout=10)
                    # Remove the stopped container so it no longer occupies a
                    # sandbox slot; the named volume keeps /root intact.
                    container.remove(force=True)
                    self._last_used.pop(sandbox_id, None)
                except Exception:
                    pass


def _truncate_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[kai] output truncated\n"
