from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from .auth import Token, TokenStore
from .docker_mgr import (
    MAX_FS_BYTES,
    DockerManager,
    JobLimitError,
    SandboxLimitError,
    sandbox_id_for,
)

MAX_JSON_BODY_BYTES = 1_000_000
MAX_INFLIGHT_V1 = int(os.environ.get("MAX_INFLIGHT_V1", "32"))
_inflight_v1 = asyncio.Semaphore(MAX_INFLIGHT_V1)
from .models import (
    AcquireRequest,
    ErrorResponse,
    ExecRequest,
    ExecResponse,
    FsEntry,
    FsListResponse,
    FsWriteRequest,
    JobResponse,
    JobStartRequest,
    SandboxStatusResponse,
)

TOKENS_FILE = Path(os.environ.get("TOKENS_FILE", "/data/tokens.txt"))
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "kai-sandbox:local")
NETWORK_NAME = os.environ.get("NETWORK_NAME", "kai_sandbox_net")
IDLE_TIMEOUT_SECONDS = int(os.environ.get("IDLE_TIMEOUT_SECONDS", "3600"))

app = FastAPI(
    title="Kai Remote Sandbox Orchestrator",
    version="0.1.0",
    description="One Debian sandbox container per user token + device_id, with full root.",
)

tokens = TokenStore(TOKENS_FILE)
_docker_mgr: DockerManager | None = None


def get_docker_mgr() -> DockerManager:
    """Lazy so unit tests can import the app without a Docker daemon."""
    global _docker_mgr
    if _docker_mgr is None:
        _docker_mgr = DockerManager(
            image=SANDBOX_IMAGE,
            network_name=NETWORK_NAME,
            idle_timeout_seconds=IDLE_TIMEOUT_SECONDS,
        )
    return _docker_mgr


def current_token(authorization: str | None = Header(default=None)) -> Token:
    try:
        return tokens.authenticate(authorization)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.middleware("http")
async def auth_and_size_gate(request: Request, call_next):
    """Authenticate /v1 before body buffering; bound concurrent /v1 work and body size."""
    path = request.url.path
    if not path.startswith("/v1/"):
        return await call_next(request)

    try:
        tokens.authenticate(request.headers.get("authorization"))
    except PermissionError as exc:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            size = int(content_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "invalid content-length"})
        limit = MAX_FS_BYTES + 65_536 if "/fs/" in path else MAX_JSON_BODY_BYTES
        if size > limit:
            return JSONResponse(
                status_code=413,
                content={"detail": f"payload exceeds {limit} bytes"},
            )

    # Admit before FastAPI buffers the body so concurrent oversized/chunked
    # requests cannot unbounded-grow orchestrator RSS.
    try:
        await asyncio.wait_for(_inflight_v1.acquire(), timeout=0.05)
    except TimeoutError:
        return JSONResponse(
            status_code=429,
            content={"detail": f"at most {MAX_INFLIGHT_V1} concurrent /v1 requests"},
        )
    try:
        return await call_next(request)
    finally:
        _inflight_v1.release()


_health_lock = threading.Lock()
_health_hits: list[float] = []
MAX_HEALTHZ_PER_SECOND = int(os.environ.get("MAX_HEALTHZ_PER_SECOND", "30"))


@app.get("/healthz")
def healthz() -> dict[str, str]:
    now = time.time()
    with _health_lock:
        # Drop samples older than 1s; reject floods that would only grow access logs.
        alive = [t for t in _health_hits if now - t < 1.0]
        if len(alive) >= MAX_HEALTHZ_PER_SECOND:
            raise HTTPException(status_code=429, detail="healthz rate limited")
        alive.append(now)
        _health_hits[:] = alive
    return {"status": "ok"}


@app.post(
    "/v1/sandboxes/acquire",
    response_model=SandboxStatusResponse,
    responses={401: {"model": ErrorResponse}},
)
async def acquire(request: Request, token: Token = Depends(current_token)) -> SandboxStatusResponse:
    raw = await _read_body_capped(request, MAX_JSON_BODY_BYTES)
    try:
        body = AcquireRequest.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid JSON body: {exc}") from exc
    try:
        info = await asyncio.to_thread(get_docker_mgr().acquire, token.token_id, body.device_id)
        return SandboxStatusResponse(**info)
    except SandboxLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/v1/sandboxes/{device_id}", response_model=SandboxStatusResponse)
def status(device_id: str, token: Token = Depends(current_token)) -> SandboxStatusResponse:
    return SandboxStatusResponse(**get_docker_mgr().status(token.token_id, device_id))


@app.delete("/v1/sandboxes/{device_id}")
def destroy(
    device_id: str,
    wipe: bool = Query(default=False),
    token: Token = Depends(current_token),
) -> dict[str, bool]:
    get_docker_mgr().destroy(token.token_id, device_id, wipe_volume=wipe)
    return {"ok": True}


@app.post("/v1/sandboxes/{device_id}/exec", response_model=ExecResponse)
async def exec_command(
    device_id: str,
    request: Request,
    token: Token = Depends(current_token),
) -> ExecResponse:
    raw = await _read_body_capped(request, MAX_JSON_BODY_BYTES)
    try:
        body = ExecRequest.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid JSON body: {exc}") from exc
    try:
        result = await asyncio.to_thread(
            get_docker_mgr().exec_command,
            token.token_id,
            device_id,
            body.command,
            body.session_id,
            body.timeout,
            body.working_dir,
            body.env,
            body.fresh,
        )
        return ExecResponse(**result)
    except SandboxLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/v1/sandboxes/{device_id}/sessions/{session_id}/close")
def close_session(
    device_id: str,
    session_id: str,
    token: Token = Depends(current_token),
) -> dict[str, bool]:
    get_docker_mgr().close_session(token.token_id, device_id, session_id)
    return {"ok": True}


@app.post("/v1/sandboxes/{device_id}/jobs", response_model=JobResponse)
async def start_job(
    device_id: str,
    request: Request,
    token: Token = Depends(current_token),
) -> JobResponse:
    raw = await _read_body_capped(request, MAX_JSON_BODY_BYTES)
    try:
        body = JobStartRequest.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid JSON body: {exc}") from exc
    try:
        job = await asyncio.to_thread(
            get_docker_mgr().start_job,
            token.token_id,
            device_id,
            body.command,
            body.working_dir,
            body.env,
        )
    except (JobLimitError, SandboxLimitError) as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JobResponse(
        job_id=job.job_id,
        status=job.status,
        command=job.command,
        exit_code=job.exit_code,
        stdout=job.stdout,
        stderr=job.stderr,
    )


@app.get("/v1/sandboxes/{device_id}/jobs", response_model=list[JobResponse])
def list_jobs(device_id: str, token: Token = Depends(current_token)) -> list[JobResponse]:
    sandbox_id = sandbox_id_for(token.token_id, device_id)
    return [
        JobResponse(
            job_id=j.job_id,
            status=j.status,
            command=j.command,
            exit_code=j.exit_code,
            stdout=j.stdout,
            stderr=j.stderr,
        )
        for j in get_docker_mgr().list_jobs(sandbox_id)
    ]


@app.get("/v1/sandboxes/{device_id}/jobs/{job_id}", response_model=JobResponse)
def get_job(device_id: str, job_id: str, token: Token = Depends(current_token)) -> JobResponse:
    job = get_docker_mgr().get_job(job_id)
    if job is None or job.sandbox_id != sandbox_id_for(token.token_id, device_id):
        raise HTTPException(status_code=404, detail="job not found")
    return JobResponse(
        job_id=job.job_id,
        status=job.status,
        command=job.command,
        exit_code=job.exit_code,
        stdout=job.stdout,
        stderr=job.stderr,
    )


@app.post("/v1/sandboxes/{device_id}/jobs/{job_id}/kill", response_model=JobResponse)
def kill_job(device_id: str, job_id: str, token: Token = Depends(current_token)) -> JobResponse:
    job = get_docker_mgr().get_job(job_id)
    if job is None or job.sandbox_id != sandbox_id_for(token.token_id, device_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = get_docker_mgr().kill_job(job_id) or job
    return JobResponse(
        job_id=job.job_id,
        status=job.status,
        command=job.command,
        exit_code=job.exit_code,
        stdout=job.stdout,
        stderr=job.stderr,
    )


@app.delete("/v1/sandboxes/{device_id}/jobs/{job_id}")
def remove_job(device_id: str, job_id: str, token: Token = Depends(current_token)) -> dict[str, bool]:
    job = get_docker_mgr().get_job(job_id)
    if job is None or job.sandbox_id != sandbox_id_for(token.token_id, device_id):
        raise HTTPException(status_code=404, detail="job not found")
    get_docker_mgr().remove_job(job_id)
    return {"ok": True}


@app.get("/v1/sandboxes/{device_id}/fs", response_model=FsListResponse)
def list_fs(
    device_id: str,
    path: str = Query(default="/root"),
    token: Token = Depends(current_token),
) -> FsListResponse:
    _assert_safe_path(path)
    try:
        entries = get_docker_mgr().list_fs(token.token_id, device_id, path)
        return FsListResponse(path=path, entries=[FsEntry(**e) for e in entries])
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/v1/sandboxes/{device_id}/fs/raw")
def read_fs(
    device_id: str,
    path: str = Query(...),
    max_bytes: int = Query(default=512_000, ge=1, le=5_000_000),
    token: Token = Depends(current_token),
) -> Response:
    _assert_safe_path(path)
    try:
        data = get_docker_mgr().read_fs(token.token_id, device_id, path, max_bytes)
        return Response(content=data, media_type="application/octet-stream")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put("/v1/sandboxes/{device_id}/fs/raw")
async def write_fs_raw(
    device_id: str,
    request: Request,
    path: str = Query(...),
    token: Token = Depends(current_token),
) -> dict[str, bool]:
    _assert_safe_path(path)
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_FS_BYTES:
                raise HTTPException(status_code=413, detail=f"payload exceeds {MAX_FS_BYTES} bytes")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content-length") from exc
    # Stream with a hard cap so chunked / missing Content-Length cannot buffer unbounded.
    body = await _read_body_capped(request, MAX_FS_BYTES)
    try:
        # Docker calls are blocking — keep them off the event loop.
        await asyncio.to_thread(get_docker_mgr().write_fs, token.token_id, device_id, path, body)
        return {"ok": True}
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/v1/sandboxes/{device_id}/fs/text")
async def write_fs_text(
    device_id: str,
    request: Request,
    token: Token = Depends(current_token),
) -> dict[str, bool]:
    # Cap before JSON parse — Starlette would otherwise buffer an unbounded body.
    # Allow a small JSON-envelope overhead above the file-content cap.
    raw = await _read_body_capped(request, MAX_FS_BYTES + 65_536)
    try:
        body = FsWriteRequest.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid JSON body: {exc}") from exc
    _assert_safe_path(body.path)
    try:
        encoded = body.content.encode(body.encoding)
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=f"unknown encoding: {body.encoding}") from exc
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=400, detail="content encoding failed") from exc
    if len(encoded) > MAX_FS_BYTES:
        raise HTTPException(status_code=413, detail=f"payload exceeds {MAX_FS_BYTES} bytes")
    try:
        await asyncio.to_thread(
            get_docker_mgr().write_fs,
            token.token_id,
            device_id,
            body.path,
            encoded,
        )
        return {"ok": True}
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/v1/sandboxes/{device_id}/fs")
def delete_fs(
    device_id: str,
    path: str = Query(...),
    recursive: bool = Query(default=False),
    token: Token = Depends(current_token),
) -> dict[str, bool]:
    _assert_safe_path(path)
    try:
        get_docker_mgr().delete_fs(token.token_id, device_id, path, recursive)
        return {"ok": True}
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/v1/sandboxes/{device_id}/fs/rename")
def rename_fs(
    device_id: str,
    path: str = Query(...),
    new_name: str = Query(...),
    token: Token = Depends(current_token),
) -> dict[str, str]:
    _assert_safe_path(path)
    try:
        dest = get_docker_mgr().rename_fs(token.token_id, device_id, path, new_name)
        return {"path": dest}
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _assert_safe_path(path: str) -> None:
    if not path.startswith("/"):
        raise HTTPException(status_code=400, detail="path must be absolute")
    if "/../" in path or path.endswith("/..") or path == "/..":
        raise HTTPException(status_code=400, detail="path traversal rejected")


async def _read_body_capped(request: Request, max_bytes: int) -> bytes:
    """Accumulate at most [max_bytes]; abort with 413 as soon as the cap is exceeded."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=f"payload exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@app.exception_handler(Exception)
async def unhandled(_: object, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": str(exc)})
