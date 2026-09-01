from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Hashable, Mapping
from typing import Protocol
from urllib.parse import parse_qsl


class RequestBodyRejected(ValueError):
    """公开请求体不满足固定资源或编码约束。"""


class StreamingRequest(Protocol):
    headers: Mapping[str, str]

    def stream(self) -> AsyncIterator[bytes]: ...


async def read_limited_body(request: StreamingRequest, limit: int) -> bytes:
    """流式读取请求体，绝不累计超过 ``limit`` 字节。"""
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise RequestBodyRejected("Content-Length 无效") from exc
        if content_length < 0 or content_length > limit:
            raise RequestBodyRejected("请求体过大")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if len(chunk) > limit - total:
            raise RequestBodyRejected("请求体过大")
        if chunk:
            chunks.append(chunk)
            total += len(chunk)
    return b"".join(chunks)


def parse_unique_form_body(body: bytes, max_fields: int) -> dict[str, str]:
    """严格解析表单，并把可预期的客户端错误归一为安静拒绝。"""
    try:
        pairs = parse_qsl(
            body.decode("utf-8", errors="strict"),
            keep_blank_values=True,
            max_num_fields=max_fields,
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise RequestBodyRejected("表单编码或字段数量无效") from exc
    if len({key for key, _ in pairs}) != len(pairs):
        raise RequestBodyRejected("表单包含重复字段")
    return dict(pairs)


class AsyncCapacity:
    """无等待队列的异步并发槽；满载时调用方应快速拒绝。"""

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity 必须大于零")
        self.capacity = capacity
        self._active = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            if self._active >= self.capacity:
                return False
            self._active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            if self._active <= 0:
                raise RuntimeError("并发槽释放次数超过获取次数")
            self._active -= 1


class SlidingWindowRateLimiter:
    """会自动清理过期键的进程内滑动窗口限流器。"""

    def __init__(self, limit: int, window_seconds: float):
        if limit < 1 or window_seconds <= 0:
            raise ValueError("限流参数必须大于零")
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[Hashable, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: Hashable, *, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else now
        cutoff = timestamp - self.window_seconds
        async with self._lock:
            self._prune(cutoff)
            events = self._events.setdefault(key, deque())
            if len(events) >= self.limit:
                return False
            events.append(timestamp)
            return True

    async def refund(self, key: Hashable) -> None:
        async with self._lock:
            events = self._events.get(key)
            if not events:
                return
            events.pop()
            if not events:
                self._events.pop(key, None)

    def _prune(self, cutoff: float) -> None:
        expired_keys: list[Hashable] = []
        for key, events in self._events.items():
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                expired_keys.append(key)
        for key in expired_keys:
            self._events.pop(key, None)
