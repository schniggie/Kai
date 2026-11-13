from app.docker_mgr import (
    MAX_ACTIVE_JOBS_PER_SANDBOX,
    MAX_FS_BYTES,
    MAX_FS_LIST_ENTRIES,
    MAX_JOB_OUTPUT_CHARS,
    MAX_SANDBOX_CONTAINERS,
    _truncate_chars,
)


def test_truncate_chars_noop_under_limit() -> None:
    assert _truncate_chars("hello", 100) == "hello"


def test_truncate_chars_marks_overflow() -> None:
    out = _truncate_chars("x" * 50, 10)
    assert out.startswith("x" * 10)
    assert "[kai] output truncated" in out


def test_cap_constants_are_finite() -> None:
    assert MAX_FS_BYTES == 5_000_000
    assert MAX_FS_LIST_ENTRIES == 2_000
    assert MAX_JOB_OUTPUT_CHARS == 100_000
    assert MAX_ACTIVE_JOBS_PER_SANDBOX == 4
    assert MAX_SANDBOX_CONTAINERS >= 1
