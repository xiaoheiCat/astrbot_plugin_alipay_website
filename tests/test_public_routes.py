from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest


class FakeRoute:
    def __init__(self, path: str):
        self.path = path


class FakeRouter:
    def __init__(self):
        # 故意使用不同的通配参数名，确保插件不依赖 AstrBot 的内部路由名称。
        self.routes = [FakeRoute("/api/example"), FakeRoute("/{anything:path}")]


class FakeApp:
    def __init__(self):
        self.router = FakeRouter()

    def add_api_route(self, path, _endpoint, **_kwargs):
        self.router.routes.append(FakeRoute(path))


class FakeHTTPResponse:
    def __init__(self, content=None, *, status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


def load_public_routes(monkeypatch: pytest.MonkeyPatch, app: FakeApp):
    fastapi = types.ModuleType("fastapi")
    fastapi.Request = object
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse = FakeHTTPResponse
    responses.PlainTextResponse = FakeHTTPResponse
    responses.Response = FakeHTTPResponse
    routing = types.ModuleType("starlette.routing")
    routing.BaseRoute = object

    server = types.ModuleType("astrbot.dashboard.server")
    server.APP = types.SimpleNamespace(_app=app)
    dashboard = types.ModuleType("astrbot.dashboard")
    dashboard.server = server
    astrbot = types.ModuleType("astrbot")
    astrbot.dashboard = dashboard

    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", responses)
    monkeypatch.setitem(sys.modules, "starlette.routing", routing)
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.dashboard", dashboard)
    monkeypatch.setitem(sys.modules, "astrbot.dashboard.server", server)

    path = Path(__file__).resolve().parents[1] / "public_routes.py"
    spec = importlib.util.spec_from_file_location("public_routes_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_public_routes_are_promoted_ahead_of_unknown_spa_catchall(monkeypatch):
    app = FakeApp()
    module = load_public_routes(monkeypatch, app)

    async def endpoint(_request):
        return None

    registrar = module.PublicRouteRegistrar()
    await registrar.register(endpoint, endpoint, endpoint)

    assert [route.path for route in app.router.routes] == [
        "/alipay",
        "/alipay/notify",
        "/alipay/return",
        "/api/example",
        "/{anything:path}",
    ]

    registrar.unregister()
    assert [route.path for route in app.router.routes] == [
        "/api/example",
        "/{anything:path}",
    ]


@pytest.mark.asyncio
async def test_registration_waits_for_dashboard_without_fixed_timeout(monkeypatch):
    app = FakeApp()
    module = load_public_routes(monkeypatch, app)
    server = sys.modules["astrbot.dashboard.server"]
    server.APP = None

    async def endpoint(_request):
        return None

    registrar = module.PublicRouteRegistrar()
    task = asyncio.create_task(registrar.register(endpoint, endpoint, endpoint))
    await asyncio.sleep(0)
    assert not task.done()

    server.APP = types.SimpleNamespace(_app=app)
    await task
    assert app.router.routes[0].path == "/alipay"


def test_payment_form_csp_allows_alipay_cashier_redirects(monkeypatch) -> None:
    module = load_public_routes(monkeypatch, FakeApp())

    response = module.payment_form_page("<form></form>", "https://astrpay-api.example/alipay")
    policy = response.headers["Content-Security-Policy"]

    assert "style-src 'unsafe-inline'" in policy
    assert "https://openapi.alipay.com" in policy
    assert "https://*.alipay.com" in policy
    assert "https://openapi-sandbox.dl.alipaydev.com" in policy
    assert "https://*.alipaydev.com" in policy
    assert "https://astrpay-api.example" in policy
    assert "https:" not in policy.split("form-action ", 1)[1].split()
