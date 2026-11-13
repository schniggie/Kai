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


def sandbox_id_for(token_id: str, device_id: str) -> str:
    digest = hashlib.sha256(f"{token_id}:{device_id}".encode()).hexdigest()[:16]
    return f"sbx_{digest}"


def container_name_for(sandbox_id: str) -> str:
    return f"kai-{sandbox_id}"


def volume_name_for(sandbox_id: str) -> str:
    return f"kai-{sandbox_id}-data"


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
        if not self._volume_exists(volume):
            self.client.volumes.create(name=volume)

        container = self._get_container(name)
        if container is None:
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
                ["bash", "--noprofile", "--norc", "-c", wrapped],
                timeout=timeout,
            )
            self.touch(sandbox_id)
            return {
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "exit_code": result["exit_code"],
                "timed_out": result["timed_out"],
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
                    "stdout": parsed.get("stdout", ""),
                    "stderr": parsed.get("stderr", ""),
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
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = BackgroundJob(job_id=job_id, sandbox_id=sandbox_id, command=command)
        with self._jobs_lock:
            self._jobs[job_id] = job

        def runner() -> None:
            prefixes: list[str] = []
            if working_dir:
                prefixes.append(f"cd {shlex.quote(working_dir)}")
            if env:
                for key, value in env.items():
                    if key.isidentifier():
                        prefixes.append(f"export {key}={shlex.quote(value)}")
            wrapped = " && ".join(prefixes + [f"( {command} )"]) if prefixes else command
            result = self._docker_exec(
                container,
                ["bash", "--noprofile", "--norc", "-c", wrapped],
                timeout=3600,
            )
            with self._jobs_lock:
                job.status = "finished"
                job.exit_code = result["exit_code"]
                job.stdout = result["stdout"]
                job.stderr = result["stderr"]
            self.touch(sandbox_id)

        threading.Thread(target=runner, daemon=True).start()
        self.touch(sandbox_id)
        return job

    def list_jobs(self, sandbox_id: str) -> list[BackgroundJob]:
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
            job.status = "killed"
            job.exit_code = -9
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
entries = []
try:
    for name in sorted(os.listdir(path), key=lambda n: (not os.path.isdir(os.path.join(path, n)), n.lower())):
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
        result = self._docker_exec(
            container,
            ["python3", "-c", f"import sys; p={path!r}; d=open(p,'rb').read({max_bytes+1}); sys.stdout.buffer.write(d)"],
            timeout=60,
            binary=True,
        )
        self.touch(info["sandbox_id"])
        if result["exit_code"] != 0:
            raise FileNotFoundError(result["stderr"] or "read failed")
        return result["stdout_bytes"]

    def write_fs(self, token_id: str, device_id: str, path: str, content: bytes) -> None:
        info = self.acquire(token_id, device_id)
        container = self._require_running(info["sandbox_id"])
        b64 = __import__("base64").b64encode(content).decode("ascii")
        script = (
            "import base64, os\n"
            f"path = {path!r}\n"
            "os.makedirs(os.path.dirname(path) or '.', exist_ok=True)\n"
            f"open(path, 'wb').write(base64.b64decode({b64!r}))\n"
        )
        result = self._docker_exec(container, ["python3", "-c", script], timeout=60)
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
    ) -> dict[str, Any]:
        # docker-py exec_run has no timeout; use the low-level API + deadline.
        exec_id = self.client.api.exec_create(container.id, cmd, stdout=True, stderr=True)["Id"]
        output = self.client.api.exec_start(exec_id, demux=True)
        # exec_start is blocking; demux returns (stdout, stderr) once finished.
        # For long jobs we accept docker's blocking behavior; caller chooses timeout
        # for session commands by asking sessiond to enforce it.
        stdout_b, stderr_b = output if isinstance(output, tuple) else (output, b"")
        stdout_b = stdout_b or b""
        stderr_b = stderr_b or b""
        inspect = self.client.api.exec_inspect(exec_id)
        exit_code = int(inspect.get("ExitCode") or 0)
        if binary:
            return {
                "stdout_bytes": stdout_b,
                "stdout": "",
                "stderr": stderr_b.decode("utf-8", "replace"),
                "exit_code": exit_code,
                "timed_out": False,
            }
        return {
            "stdout": stdout_b.decode("utf-8", "replace"),
            "stderr": stderr_b.decode("utf-8", "replace"),
            "exit_code": exit_code,
            "timed_out": False,
        }

    def _idle_reaper(self) -> None:
        while True:
            time.sleep(60)
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
                except Exception:
                    pass
