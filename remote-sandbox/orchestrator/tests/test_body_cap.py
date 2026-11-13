import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.main import _read_body_capped


def _request_from_chunks(chunks: list[bytes]) -> Request:
    index = {"i": 0}

    async def receive() -> dict:
        i = index["i"]
        if i >= len(chunks):
            return {"type": "http.request", "body": b"", "more_body": False}
        body = chunks[i]
        index["i"] = i + 1
        more = index["i"] < len(chunks)
        return {"type": "http.request", "body": body, "more_body": more}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "headers": [],
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
        "scheme": "http",
    }
    return Request(scope, receive)


def test_read_body_capped_accepts_under_limit() -> None:
    req = _request_from_chunks([b"hello", b" ", b"world"])
    body = asyncio.run(_read_body_capped(req, 100))
    assert body == b"hello world"


def test_read_body_capped_rejects_over_limit() -> None:
    req = _request_from_chunks([b"x" * 50, b"y" * 60])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_read_body_capped(req, 100))
    assert exc.value.status_code == 413


def test_read_body_capped_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main

    monkeypatch.setattr(main, "BODY_READ_TIMEOUT_SECONDS", 0.05)

    async def _slow_chunks() -> None:
        index = {"i": 0}
        chunks = [b"a", b"b"]

        async def receive() -> dict:
            i = index["i"]
            if i == 0:
                index["i"] = 1
                await asyncio.sleep(0.2)
                return {"type": "http.request", "body": chunks[0], "more_body": True}
            if i == 1:
                index["i"] = 2
                return {"type": "http.request", "body": chunks[1], "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "PUT",
            "headers": [],
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
            "scheme": "http",
        }
        from starlette.requests import Request

        req = Request(scope, receive)
        with pytest.raises(HTTPException) as exc:
            await main._read_body_capped(req, 100)
        assert exc.value.status_code == 408

    asyncio.run(_slow_chunks())
