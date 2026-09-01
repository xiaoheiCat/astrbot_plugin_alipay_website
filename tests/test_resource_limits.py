from __future__ import annotations

import pytest

from resource_limits import (
    AsyncCapacity,
    RequestBodyRejected,
    SlidingWindowRateLimiter,
    parse_unique_form_body,
    read_limited_body,
)


class FakeRequest:
    def __init__(self, chunks: list[bytes], content_length: str | None = None):
        self.chunks = chunks
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = content_length
        self.chunks_read = 0

    async def stream(self):
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk


@pytest.mark.asyncio
async def test_limited_body_accepts_exact_limit() -> None:
    request = FakeRequest([b"a" * 32_768, b"b" * 32_768])

    body = await read_limited_body(request, 65_536)

    assert len(body) == 65_536


@pytest.mark.asyncio
async def test_limited_body_rejects_stream_that_lies_about_length() -> None:
    request = FakeRequest([b"a" * 40_000, b"b" * 25_537], content_length="1")

    with pytest.raises(RequestBodyRejected, match="过大"):
        await read_limited_body(request, 65_536)

    assert request.chunks_read == 2


@pytest.mark.asyncio
async def test_limited_body_rejects_declared_oversize_without_reading() -> None:
    request = FakeRequest([b"not-read"], content_length="65537")

    with pytest.raises(RequestBodyRejected, match="过大"):
        await read_limited_body(request, 65_536)

    assert request.chunks_read == 0


@pytest.mark.parametrize(
    "body",
    [
        b"\xff",
        b"key=%FF",
        b"&".join(f"key{index}=value".encode() for index in range(101)),
        b"key=first&key=second",
    ],
)
def test_malformed_forms_are_normalized_to_quiet_rejection(body: bytes) -> None:
    with pytest.raises(RequestBodyRejected):
        parse_unique_form_body(body, 100)


def test_valid_unique_form_is_preserved() -> None:
    assert parse_unique_form_body(b"a=1&b=2", 100) == {"a": "1", "b": "2"}


@pytest.mark.asyncio
async def test_async_capacity_fails_fast_without_queue() -> None:
    capacity = AsyncCapacity(1)

    assert await capacity.acquire()
    assert not await capacity.acquire()
    await capacity.release()
    assert await capacity.acquire()


@pytest.mark.asyncio
async def test_sliding_window_limiter_expires_old_keys_and_supports_refund() -> None:
    limiter = SlidingWindowRateLimiter(1, 10)

    assert await limiter.allow("session-a", now=0)
    assert not await limiter.allow("session-a", now=1)
    await limiter.refund("session-a")
    assert await limiter.allow("session-a", now=1)
    assert await limiter.allow("session-b", now=20)
    assert "session-a" not in limiter._events
