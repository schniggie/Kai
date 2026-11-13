from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AcquireRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)


class SandboxStatusResponse(BaseModel):
    sandbox_id: str
    device_id: str
    status: str
    container_name: str
    ready: bool
    disk_usage_bytes: int = 0


class ExecRequest(BaseModel):
    command: str = Field(min_length=1, max_length=100_000)
    session_id: str = Field(default="default", max_length=128)
    timeout: int = Field(default=30, ge=1, le=300)
    working_dir: str | None = None
    env: dict[str, str] | None = None
    fresh: bool = False


class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False
    session_id: str


class JobStartRequest(BaseModel):
    command: str = Field(min_length=1, max_length=100_000)
    working_dir: str | None = None
    env: dict[str, str] | None = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    command: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


class FsEntry(BaseModel):
    name: str
    path: str
    is_directory: bool
    size_bytes: int
    last_modified_ms: int


class FsListResponse(BaseModel):
    path: str
    entries: list[FsEntry]


class FsWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    # Cap matches orchestrator MAX_FS_BYTES (5 MiB). Characters ≈ bytes for UTF-8
    # ASCII; non-ASCII is rejected later by the encoded-length check in the route.
    content: str = Field(default="", max_length=5_000_000)
    encoding: str = Field(default="utf-8", max_length=32)


class ErrorResponse(BaseModel):
    error: str
    detail: Any | None = None
