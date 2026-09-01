from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import BaseRoute


class PublicRouteRegistrar:
    """把公开支付路由插到 AstrBot SPA catch-all 之前。"""

    def __init__(self):
        self._app = None
        self._routes: list[BaseRoute] = []

    async def register(
        self,
        landing: Callable[[Request], Awaitable[Response]],
        notify: Callable[[Request], Awaitable[Response]],
        returned: Callable[[Request], Awaitable[Response]],
    ) -> None:
        while True:
            from astrbot.dashboard import server

            if server.APP is not None:
                self._app = server.APP._app
                break
            await asyncio.sleep(0.25)

        definitions = [
            ("/alipay", landing, ["GET"], "alipay_landing"),
            ("/alipay/notify", notify, ["POST"], "alipay_notify"),
            ("/alipay/return", returned, ["GET"], "alipay_return"),
        ]
        registered: list[BaseRoute] = []
        try:
            for path, endpoint, methods, name in definitions:
                routes = self._app.router.routes
                previous_count = len(routes)
                self._app.add_api_route(
                    path,
                    endpoint,
                    methods=methods,
                    name=name,
                    include_in_schema=False,
                )
                if len(routes) != previous_count + 1:
                    raise RuntimeError(f"AstrBot 未按预期注册公开路由：{path}")

                route = routes.pop()
                # AstrBot 的 SPA 使用通配 GET 路由。公开支付 GET 路由必须位于它
                # 之前；直接按声明顺序置顶，避免依赖宿主内部的通配路由名称。
                routes.insert(len(registered), route)
                registered.append(route)
        except BaseException:
            for route in registered:
                if route in self._app.router.routes:
                    self._app.router.routes.remove(route)
            self._app = None
            raise
        self._routes.extend(registered)

    def unregister(self) -> None:
        if self._app is None:
            return
        for route in self._routes:
            if route in self._app.router.routes:
                self._app.router.routes.remove(route)
        self._routes.clear()
        self._app = None


def page(title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{margin:0;background:#f5f7fa;font:16px/1.6 system-ui;color:#1f2937}}
.card{{max-width:520px;margin:12vh auto;padding:32px;background:white;border-radius:16px;
box-shadow:0 8px 30px #0001;text-align:center}}button{{border:0;border-radius:9px;padding:12px 24px;
background:#1677ff;color:white;font-size:16px}}small{{color:#6b7280}}</style></head>
<body><main class="card">{body}</main></body></html>"""
    return HTMLResponse(
        document,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


def payment_form_page(signed_form: str, callback_base: str) -> HTMLResponse:
    # signed_form 由支付宝官方 SDK 生成；仅允许其自动提交到支付宝网关。
    callback_parts = urlsplit(callback_base)
    if callback_parts.scheme not in {"http", "https"} or not callback_parts.netloc:
        raise ValueError("支付回调地址无效")
    callback_origin = urlunsplit((callback_parts.scheme, callback_parts.netloc, "", "", ""))
    form_action_sources = (
        "https://openapi.alipay.com",
        "https://alipay.com",
        "https://*.alipay.com",
        "https://openapi-sandbox.dl.alipaydev.com",
        "https://alipaydev.com",
        "https://*.alipaydev.com",
        callback_origin,
    )
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>正在前往支付宝</title></head>
<body><p>正在将你重定向到支付宝收银台…</p>{signed_form}</body></html>"""
    return HTMLResponse(
        document,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; base-uri 'none'; script-src 'unsafe-inline'; "
                f"style-src 'unsafe-inline'; form-action {' '.join(form_action_sources)}"
            ),
        },
    )


def alipay_ack(success: bool) -> PlainTextResponse:
    return PlainTextResponse(
        "success" if success else "fail",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
